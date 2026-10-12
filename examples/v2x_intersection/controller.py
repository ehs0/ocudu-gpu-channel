"""Vehicle A's communication-aware longitudinal controller (pure Python).

The lane is followed by the simulator (SUMO keeps the vehicle on its route),
so the controller decides only acceleration. Two layers:

* **Risk layer** (runs every control tick): from the best estimate of
  vehicle B — the local line-of-sight sensor when B is visible, otherwise the
  latest V2X message extrapolated to now — compute the arrival times at the
  conflict point

      T_A = d_A / max(v_A, eps),  T_B = d_B / max(v_B, eps)

  and flag a conflict when both approach and |T_A - T_B| < `conflict_window_s`,
  or when B is inside the crossing. Once flagged, A keeps yielding until B has
cleared the crossing. A conflict either lowers the target speed
  so A arrives after B has cleared (WARNING) or, when that is no longer
  possible with comfortable deceleration, brakes to the stop line (BRAKING,
  then STOP; BRAKING is not relaxed back to WARNING while B approaches).
* **Speed layer**: a PI controller tracks the target speed; BRAKING uses the
  deceleration needed to stop at the line directly, capped at `decel_max`.

When the local sensor sees B's approach lane and B is not on it, the
crossing counts as clear. When there is no information at all (no V2X for
`stale_after_s`, B's approach out of view) the `fallback` decides: `cautious` (default) limits the speed so A can still stop
at the line with comfortable deceleration, i.e. it does not assume the road
is clear; `none` drives on as if the crossing were empty (for comparison only).

States: CRUISE, WARNING, BRAKING, STOP, plus PASSED once A is beyond the
stop line without braking for B (it can no longer stop before the crossing
and drives on). A that overshoots the line while braking for B keeps braking
at `decel_max`; it never accelerates into B.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import layout

EPS_SPEED_MPS = 0.1


@dataclass(frozen=True)
class VehicleState:
    vehicle_id: str
    simulation_time: float
    wall_time: float
    position: tuple[float, float, float]
    orientation: tuple[float, float, float]      # roll, pitch, yaw (rad, yaw from +x towards +y)
    velocity: tuple[float, float, float]
    speed_mps: float


@dataclass(frozen=True)
class Estimate:
    """What A believes about B at the current simulation time."""

    position: tuple[float, float]
    velocity: tuple[float, float]
    source: str             # "v2x" or "sensor"
    age_s: float            # how old the underlying observation is


@dataclass
class Params:
    cruise_speed_mps: float = layout.CRUISE_SPEED_MPS
    accel_max: float = 2.5
    decel_comfort: float = 3.0
    decel_max: float = 6.0              # physical limit applied by the world as well
    kp: float = 1.2
    ki: float = 0.2
    conflict_window_s: float = 2.5
    horizon_s: float = 10.0
    clear_margin_m: float = 6.0         # B counts as clear this far past the conflict point
    lateral_tolerance_m: float = 3.0    # B's path must pass this close to the conflict point
    stale_after_s: float = 0.3          # three V2X periods at 10 Hz
    fallback: str = "cautious"          # or "none"
    use_v2x: bool = True

    def __post_init__(self) -> None:
        if self.fallback not in ("cautious", "none"):
            raise ValueError("fallback must be 'cautious' or 'none'")


@dataclass(frozen=True)
class ControlOutput:
    accel_mps2: float
    throttle: float                 # accel / accel_max, 0..1
    brake: float                    # -accel / decel_max, 0..1
    steering: None                  # lane keeping is SUMO's; the controller never steers
    state: str
    target_speed_mps: float
    reason: str
    info: str                       # see select_estimate
    d_a_m: float
    d_b_m: float | None
    t_a_s: float | None
    t_b_s: float | None
    info_age_s: float | None


@dataclass
class Controller:
    conflict_point: tuple[float, float]
    stop_line_gap_m: float
    params: Params = field(default_factory=Params)
    state: str = "CRUISE"
    _integral: float = 0.0
    # Set by the first conflict and kept until B has cleared the crossing (or
    # left B's approach), so A does not re-accelerate as soon as slowing down
    # has pushed |T_A - T_B| outside the window, then brake again.
    _yielding: bool = False

    def step(self, ego: VehicleState, estimate: Estimate | None, dt: float,
             *, info: str) -> ControlOutput:
        p = self.params
        heading = (math.cos(ego.orientation[2]), math.sin(ego.orientation[2]))
        cp = self.conflict_point
        d_a = (cp[0] - ego.position[0]) * heading[0] + (cp[1] - ego.position[1]) * heading[1]
        v_a = ego.speed_mps
        d_stop = d_a - self.stop_line_gap_m
        target = p.cruise_speed_mps
        reason = "clear"
        d_b = t_a = t_b = None
        braking_decel: float | None = None
        conflict = False
        if estimate is not None:
            d_b, t_a, t_b, conflict, reason = self._assess(d_a, v_a, estimate)
            self._yielding = conflict
        elif info == "sensor_clear":
            self._yielding = False

        if d_stop <= 0.0 and conflict and self.state in ("BRAKING", "STOP"):
            # Overshot the line while braking for B: keep braking, never accelerate into B.
            self.state = "BRAKING" if v_a > EPS_SPEED_MPS else "STOP"
            target = 0.0
            braking_decel = p.decel_max
            reason = f"past stop line while braking: {reason}"
        elif d_stop <= 0.0 and self.state != "STOP":
            # At or past the line and not braking for B: stopping now would block the crossing.
            self.state = "PASSED"
            reason = "beyond stop line"
        elif info == "sensor_clear":
            self.state = "CRUISE"
            reason = "crossing approach in view and empty"
        elif estimate is not None:
            if conflict:
                # Speed at which A arrives once B has cleared, if A can still make it.
                clear_time = (d_b + p.clear_margin_m) / max(_norm(estimate.velocity), EPS_SPEED_MPS)
                yield_speed = d_a / (clear_time + p.conflict_window_s)
                needed = v_a * v_a / (2.0 * d_stop)
                if (needed < 0.5 * p.decel_comfort and yield_speed > 1.0
                        and self.state not in ("BRAKING", "STOP")):
                    self.state = "WARNING"
                    target = min(target, yield_speed)
                else:
                    self.state = "BRAKING" if v_a > EPS_SPEED_MPS else "STOP"
                    target = 0.0
                    braking_decel = min(max(needed, p.decel_comfort), p.decel_max)
            else:
                self.state = "CRUISE"
        else:
            if p.fallback == "cautious":
                limit = math.sqrt(2.0 * p.decel_comfort * d_stop)
                target = min(target, limit)
                reason = f"no fresh information ({info}): stoppable speed {limit:.1f} m/s"
                if limit < p.cruise_speed_mps:
                    self.state = "WARNING" if v_a > EPS_SPEED_MPS else "STOP"
                else:
                    self.state = "CRUISE"
            else:
                self.state = "CRUISE"
                reason = f"no fresh information ({info}): fallback none"

        if braking_decel is not None:
            accel = -braking_decel
            self._integral = 0.0
        else:
            error = target - v_a
            self._integral = max(-5.0, min(5.0, self._integral + error * dt))
            accel = p.kp * error + p.ki * self._integral
        accel = max(-p.decel_max, min(p.accel_max, accel))
        if self.state == "STOP" and target == 0.0:
            accel = -p.decel_comfort if v_a > 0.0 else 0.0
        return ControlOutput(
            accel_mps2=accel,
            throttle=max(0.0, accel) / p.accel_max,
            brake=max(0.0, -accel) / p.decel_max,
            steering=None,
            state=self.state,
            target_speed_mps=target,
            reason=reason,
            info=info,
            d_a_m=d_a,
            d_b_m=d_b,
            t_a_s=t_a,
            t_b_s=t_b,
            info_age_s=None if estimate is None else estimate.age_s,
        )

    def _assess(self, d_a: float, v_a: float, estimate: Estimate):
        """(d_B, T_A, T_B, conflict, reason) for the current estimate of B."""

        p = self.params
        cp = self.conflict_point
        speed_b = _norm(estimate.velocity)
        rel = (cp[0] - estimate.position[0], cp[1] - estimate.position[1])
        if speed_b < EPS_SPEED_MPS:
            # A standing B is a conflict only if it stands in the crossing.
            inside = math.hypot(*rel) < p.clear_margin_m
            return None, None, None, inside, "B stopped in crossing" if inside else "B stopped"
        u = (estimate.velocity[0] / speed_b, estimate.velocity[1] / speed_b)
        d_b = rel[0] * u[0] + rel[1] * u[1]
        lateral = abs(rel[0] * u[1] - rel[1] * u[0])
        if lateral > p.lateral_tolerance_m:
            return d_b, None, None, False, "B not heading for the crossing"
        if d_b < -p.clear_margin_m:
            return d_b, None, None, False, "B has cleared the crossing"
        t_a = d_a / max(v_a, EPS_SPEED_MPS)
        t_b = max(d_b, 0.0) / speed_b
        if d_b <= 0.0:
            return d_b, t_a, t_b, True, "B in the crossing"
        if self._yielding:
            return d_b, t_a, t_b, True, f"yielding until B clears (|T_A-T_B|={abs(t_a - t_b):.2f}s)"
        if t_a > p.horizon_s:
            return d_b, t_a, t_b, False, "A beyond horizon"
        if abs(t_a - t_b) < p.conflict_window_s:
            return d_b, t_a, t_b, True, f"|T_A-T_B|={abs(t_a - t_b):.2f}s"
        return d_b, t_a, t_b, False, f"no overlap |T_A-T_B|={abs(t_a - t_b):.2f}s"


def select_estimate(
    *,
    now_sim: float,
    sensor: tuple[tuple[float, float], tuple[float, float]] | None,
    approach_in_view: bool,
    v2x: Any | None,
    params: Params,
) -> tuple[Estimate | None, str]:
    """Best current estimate of B and the label logged as `info`.

    Priority: B seen by the local sensor ("sensor"); a fresh V2X message
    ("v2x"); the local sensor seeing B's approach empty ("sensor_clear");
    otherwise nothing ("off" when V2X is disabled, "missing" before the first
    message, "stale" after `stale_after_s` without one).

    `sensor` is (position, velocity) when B is in line of sight and range;
    `approach_in_view` is whether A sees B's approach lane at all; `v2x` is
    the newest `messages.VehicleStateMessage` received. A V2X position is
    extrapolated at constant velocity from the message's simulation time to
    now, so delivery delay is compensated and only loss (staleness) remains.
    """

    if sensor is not None:
        return Estimate(sensor[0], sensor[1], "sensor", 0.0), "sensor"
    label = "off"
    if params.use_v2x:
        label = "missing"
        if v2x is not None:
            age = now_sim - v2x.simulation_time
            label = "stale"
            if age <= params.stale_after_s:
                position = (v2x.position[0] + v2x.velocity[0] * age,
                            v2x.position[1] + v2x.velocity[1] * age)
                return Estimate(position, (v2x.velocity[0], v2x.velocity[1]), "v2x", age), "v2x"
    if approach_in_view:
        return None, "sensor_clear"
    return None, label


def _norm(v: tuple[float, float]) -> float:
    return math.hypot(v[0], v[1])
