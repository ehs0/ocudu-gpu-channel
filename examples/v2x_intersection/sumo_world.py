"""SUMO adapter: the only module that imports SUMO (libsumo, sumolib).

SUMO is not part of this repository. It is installed outside the tree by
`setup-external.sh` (Eclipse SUMO 1.28.0 from PyPI into `~/venvs/v2x`); this
module builds the intersection network from `layout.py`, runs SUMO in-process
through libsumo, and exposes the vehicles as `controller.VehicleState`.

Everything SUMO would do on its own to avoid a crash is switched off, because
the point of the demo is that vehicle A's controller decides:

* the junction is `unregulated` (no right of way, no foe checks);
* both vehicles run with speed mode 32 (ignore safe speed, acceleration
  limits and right of way inside the junction) and lane-change mode 0;
* speeds are set every step with `setSpeed`: B follows its scripted speed,
  A integrates the controller's acceleration, clipped to [-decel_max,
  accel_max] and to non-negative speed.

The network is built with `--offset.disable-normalization`, so SUMO
coordinates equal scene coordinates (checked in `tests/test_v2x_layout.py`).
SUMO only keeps the vehicles on their lanes; there is no steering command.
"""

from __future__ import annotations

import math
import os
import pathlib
import shutil
import subprocess
import time
from dataclasses import dataclass

import layout
from controller import VehicleState

try:  # SUMO lives outside the repository; see setup-external.sh.
    import libsumo
    import sumolib
except ImportError as exc:  # pragma: no cover - environment check
    raise ImportError(
        "SUMO is not importable. Run examples/v2x_intersection/setup-external.sh and use "
        "the interpreter it prints (it also sets LD_LIBRARY_PATH for SUMO's X11 libraries)."
    ) from exc

SPEED_MODE_IGNORE_ALL = 32
VEHICLE_IDS = {"A": "vehA", "B": "vehB"}


def sumo_version() -> str:
    return libsumo.getVersion()[1] if hasattr(libsumo, "getVersion") else "unknown"


def _netconvert() -> str:
    found = shutil.which("netconvert")
    if found:
        return found
    sumo_home = os.environ.get("SUMO_HOME")
    if sumo_home and (pathlib.Path(sumo_home) / "bin" / "netconvert").exists():
        return str(pathlib.Path(sumo_home) / "bin" / "netconvert")
    import sumo  # the eclipse-sumo wheel

    return str(pathlib.Path(sumo.SUMO_HOME) / "bin" / "netconvert")


def build_network(work_dir: pathlib.Path) -> pathlib.Path:
    """Write the plain node/edge XML from `layout.py` and run netconvert."""

    work_dir.mkdir(parents=True, exist_ok=True)
    nodes = ["<nodes>"]
    for node_id, (x, y) in layout.NET_NODES_M.items():
        kind = "unregulated" if node_id == "C" else "dead_end"
        nodes.append(f'  <node id="{node_id}" x="{x}" y="{y}" type="{kind}"/>')
    nodes.append("</nodes>")
    edges = ["<edges>"]
    for outer in ("S", "N", "E", "W"):
        for src, dst in ((outer, "C"), ("C", outer)):
            edges.append(
                f'  <edge id="{src}2{dst}" from="{src}" to="{dst}" numLanes="1" '
                f'speed="13.89" width="{layout.LANE_WIDTH_M}"/>'
            )
    edges.append("</edges>")
    (work_dir / "intersection.nod.xml").write_text("\n".join(nodes) + "\n", encoding="utf-8")
    (work_dir / "intersection.edg.xml").write_text("\n".join(edges) + "\n", encoding="utf-8")
    net = work_dir / "intersection.net.xml"
    subprocess.run(
        [
            _netconvert(),
            "--node-files", str(work_dir / "intersection.nod.xml"),
            "--edge-files", str(work_dir / "intersection.edg.xml"),
            "--output-file", str(net),
            "--offset.disable-normalization", "true",
            "--no-turnarounds", "true",
            "--default.junctions.radius", "4",
            "--no-warnings", "true",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return net


@dataclass(frozen=True)
class Start:
    a_position: tuple[float, float] = layout.A_START_M
    b_position: tuple[float, float] = layout.B_START_M
    a_speed_mps: float = layout.CRUISE_SPEED_MPS
    b_speed_mps: float = layout.CRUISE_SPEED_MPS


class SumoWorld:
    """Two vehicles on the layout's intersection, stepped by the caller."""

    def __init__(self, work_dir: pathlib.Path, *, step_s: float = 0.05, seed: int = 1,
                 start: Start = Start(), accel_max: float = 2.5, decel_max: float = 6.0) -> None:
        self.step_s = step_s
        self.start = start
        self.accel_max = accel_max
        self.decel_max = decel_max
        self.net_path = build_network(work_dir)
        self.net = sumolib.net.readNet(str(self.net_path))
        routes = work_dir / "intersection.rou.xml"
        a_pos = self._lane_pos("S2C_0", start.a_position)
        b_pos = self._lane_pos("E2C_0", start.b_position)
        routes.write_text(
            "\n".join([
                "<routes>",
                f'  <vType id="car" length="{layout.VEHICLE_LENGTH_M}" width="{layout.VEHICLE_WIDTH_M}" '
                f'accel="{accel_max}" decel="{decel_max}" emergencyDecel="{decel_max}" sigma="0" maxSpeed="30"/>',
                f'  <route id="routeA" edges="{" ".join(layout.ROUTE_A)}"/>',
                f'  <route id="routeB" edges="{" ".join(layout.ROUTE_B)}"/>',
                f'  <vehicle id="{VEHICLE_IDS["A"]}" type="car" route="routeA" depart="0" '
                f'departPos="{a_pos:.3f}" departSpeed="{start.a_speed_mps}" departLane="0"/>',
                f'  <vehicle id="{VEHICLE_IDS["B"]}" type="car" route="routeB" depart="0" '
                f'departPos="{b_pos:.3f}" departSpeed="{start.b_speed_mps}" departLane="0"/>',
                "</routes>",
            ]) + "\n",
            encoding="utf-8",
        )
        libsumo.start([
            "sumo",
            "--net-file", str(self.net_path),
            "--route-files", str(routes),
            "--step-length", f"{step_s}",
            "--seed", str(seed),
            "--no-step-log", "true",
            "--no-warnings", "true",
            "--collision.action", "none",
            "--time-to-teleport", "-1",
        ])
        libsumo.simulationStep()          # inserts both vehicles at t = step_s
        for vid in VEHICLE_IDS.values():
            libsumo.vehicle.setSpeedMode(vid, SPEED_MODE_IGNORE_ALL)
            libsumo.vehicle.setLaneChangeMode(vid, 0)
        self._a_speed = start.a_speed_mps
        libsumo.vehicle.setSpeed(VEHICLE_IDS["A"], self._a_speed)
        libsumo.vehicle.setSpeed(VEHICLE_IDS["B"], start.b_speed_mps)

    def _lane_pos(self, lane_id: str, xy: tuple[float, float]) -> float:
        lane = self.net.getLane(lane_id)
        pos, dist = lane.getClosestLanePosAndDist(xy)
        if dist > 0.5:
            raise ValueError(f"start {xy} is {dist:.2f} m off lane {lane_id}")
        return pos

    @property
    def simulation_time(self) -> float:
        return libsumo.simulation.getTime()

    def state(self, key: str) -> VehicleState:
        vid = VEHICLE_IDS[key]
        x, y = libsumo.vehicle.getPosition(vid)
        speed = libsumo.vehicle.getSpeed(vid)
        # SUMO angle: degrees clockwise from north. Scene yaw: radians from +x towards +y.
        yaw = math.radians(90.0 - libsumo.vehicle.getAngle(vid))
        return VehicleState(
            vehicle_id=key,
            simulation_time=self.simulation_time,
            wall_time=time.time(),
            position=(x, y, 0.0),
            orientation=(0.0, 0.0, yaw),
            velocity=(speed * math.cos(yaw), speed * math.sin(yaw), 0.0),
            speed_mps=speed,
        )

    def step(self, a_accel_mps2: float) -> float:
        """Apply A's acceleration for one step, advance SUMO; returns the applied acceleration."""

        accel = max(-self.decel_max, min(self.accel_max, a_accel_mps2))
        next_speed = max(0.0, self._a_speed + accel * self.step_s)
        applied = (next_speed - self._a_speed) / self.step_s
        self._a_speed = next_speed
        libsumo.vehicle.setSpeed(VEHICLE_IDS["A"], next_speed)
        libsumo.vehicle.setSpeed(VEHICLE_IDS["B"], self.start.b_speed_mps)
        libsumo.simulationStep()
        return applied

    def close(self) -> None:
        libsumo.close()
