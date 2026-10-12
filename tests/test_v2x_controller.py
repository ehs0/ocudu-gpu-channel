"""Vehicle A's controller: conflict detection, braking, stale-information fallback."""

import math
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "examples" / "v2x_intersection"))

import layout  # noqa: E402
from controller import Controller, Estimate, Params, VehicleState, select_estimate  # noqa: E402
from messages import VehicleStateMessage  # noqa: E402

CP = layout.CONFLICT_POINT_M
GAP = layout.STOP_LINE_GAP_M


def ego(y, speed, t=0.0):
    return VehicleState("A", t, 0.0, (CP[0], y, 0.0), (0.0, 0.0, math.pi / 2), (0.0, speed, 0.0), speed)


def b_at(x, speed=13.89, age=0.0, source="v2x"):
    return Estimate((x, CP[1]), (-speed, 0.0), source, age)


def make(**kw):
    return Controller(CP, GAP, Params(**kw))


def test_simultaneous_arrival_is_a_conflict_and_slows_a():
    c = make()
    # A 100 m out at 13.89 m/s, B 100 m out at 13.89 m/s: same arrival time.
    out = c.step(ego(CP[1] - 100.0, 13.89), b_at(CP[0] + 100.0), 0.05, info="v2x")
    assert out.state in ("WARNING", "BRAKING")
    assert out.accel_mps2 < 0.0
    assert out.t_a_s == pytest.approx(out.t_b_s, abs=0.1)


def test_late_conflict_brakes_hard_but_within_physical_limit():
    c = make()
    out = c.step(ego(CP[1] - 30.0, 13.89), b_at(CP[0] + 30.0), 0.05, info="v2x")
    assert out.state == "BRAKING"
    assert -c.params.decel_max <= out.accel_mps2 <= -c.params.decel_comfort
    assert out.brake > 0.0 and out.throttle == 0.0


def test_no_conflict_when_b_has_cleared_or_is_far_apart_in_time():
    c = make()
    out = c.step(ego(CP[1] - 60.0, 13.89), b_at(CP[0] - 10.0), 0.05, info="v2x")
    assert out.state == "CRUISE" and "cleared" in out.reason
    out = c.step(ego(CP[1] - 60.0, 13.89), b_at(CP[0] + 400.0), 0.05, info="v2x")
    assert out.state == "CRUISE"


def test_b_not_heading_for_the_crossing_is_ignored():
    c = make()
    estimate = Estimate((CP[0] + 50.0, CP[1] + 20.0), (-13.89, 0.0), "v2x", 0.0)   # parallel lane 20 m north
    out = c.step(ego(CP[1] - 50.0, 13.89), estimate, 0.05, info="v2x")
    assert out.state == "CRUISE"


def test_cautious_fallback_limits_speed_to_a_stoppable_one():
    c = make(fallback="cautious")
    d_stop = 20.0
    out = c.step(ego(CP[1] - GAP - d_stop, 13.89), None, 0.05, info="stale")
    assert out.target_speed_mps == pytest.approx(math.sqrt(2 * c.params.decel_comfort * d_stop))
    assert out.accel_mps2 < 0.0
    far = make(fallback="cautious").step(ego(CP[1] - 100.0, 13.89), None, 0.05, info="missing")
    assert far.state == "CRUISE"            # far away the stoppable speed exceeds cruise


def test_fallback_none_keeps_cruising_without_information():
    out = make(fallback="none").step(ego(CP[1] - GAP - 20.0, 13.89), None, 0.05, info="stale")
    assert out.state == "CRUISE" and out.target_speed_mps == pytest.approx(13.89)


def test_overshooting_the_line_while_braking_never_accelerates():
    c = make()
    c.step(ego(CP[1] - 20.0, 13.89), b_at(CP[0] + 15.0), 0.05, info="v2x")
    assert c.state == "BRAKING"
    out = c.step(ego(CP[1] - GAP + 1.0, 8.0), b_at(CP[0] + 8.0), 0.05, info="v2x")
    assert out.state == "BRAKING"
    assert out.accel_mps2 == pytest.approx(-c.params.decel_max)


def test_past_the_line_without_braking_drives_on():
    out = make().step(ego(CP[1] - GAP + 1.0, 13.89), None, 0.05, info="missing")
    assert out.state == "PASSED"


def test_select_estimate_priority_and_extrapolation():
    p = Params()
    msg = VehicleStateMessage("B", 1, 10.0, 1, (50.0, CP[1], 0.0), (-10.0, 0.0, 0.0))
    est, info = select_estimate(now_sim=10.2, sensor=None, approach_in_view=False, v2x=msg, params=p)
    assert info == "v2x" and est.position[0] == pytest.approx(48.0) and est.age_s == pytest.approx(0.2)
    est, info = select_estimate(now_sim=10.5, sensor=None, approach_in_view=False, v2x=msg, params=p)
    assert est is None and info == "stale"
    est, info = select_estimate(now_sim=10.5, sensor=None, approach_in_view=True, v2x=msg, params=p)
    assert est is None and info == "sensor_clear"
    est, info = select_estimate(now_sim=10.2, sensor=((20.0, CP[1]), (-10.0, 0.0)), approach_in_view=True,
                                v2x=msg, params=p)
    assert info == "sensor" and est.position == (20.0, CP[1])
    est, info = select_estimate(now_sim=10.2, sensor=None, approach_in_view=False, v2x=msg,
                                params=Params(use_v2x=False))
    assert est is None and info == "off"
    est, info = select_estimate(now_sim=0.0, sensor=None, approach_in_view=False, v2x=None, params=p)
    assert info == "missing"


def test_yielding_does_not_chatter_once_the_window_is_left():
    """Slowing down moves T_A out of the window; A must keep yielding, not re-accelerate."""
    c = make()
    first = c.step(ego(CP[1] - 100.0, 13.89), b_at(CP[0] + 100.0), 0.05, info="v2x")
    assert first.state == "WARNING"
    # A has slowed so far that |T_A - T_B| > window; B is still approaching.
    later = c.step(ego(CP[1] - 60.0, 6.0), b_at(CP[0] + 40.0), 0.05, info="v2x")
    assert abs(later.t_a_s - later.t_b_s) > c.params.conflict_window_s
    assert later.state in ("WARNING", "BRAKING") and later.target_speed_mps < 13.89
    cleared = c.step(ego(CP[1] - 30.0, 6.0), b_at(CP[0] - 10.0), 0.05, info="v2x")
    assert cleared.state == "CRUISE"
