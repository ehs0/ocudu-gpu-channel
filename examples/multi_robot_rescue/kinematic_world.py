"""Development world: 2-D kinematic differential-drive robots in the layout (numpy, no physics engine).

Same interface as `isaac_world.py` (the Isaac Sim adapter), so sim.py, the
robots and the tests run without Isaac. Kinematic, not dynamic: a commanded
body velocity is followed with acceleration limits; a step that would overlap
a wall, rubble, a survivor or another robot is not taken and counts as a
contact. The LiDAR is `sensors.cast`. Results from this world are labelled
`world: kinematic` and are development results.
"""

from __future__ import annotations

import math

import numpy as np

import layout
import sensors

MAX_V_MPS = 0.6
MAX_W_RPS = 1.5
ACCEL_MPS2 = 1.0
ALPHA_RPS2 = 3.0


class KinematicWorld:
    name = "kinematic"

    def __init__(self, survivors: tuple[tuple[float, float], ...], robot_ids=layout.ROBOT_IDS):
        self.survivors = [tuple(s) for s in survivors]
        self.robot_ids = tuple(robot_ids)
        self.pose = {rid: list(layout.SPAWN_POSES[rid]) for rid in self.robot_ids}
        self.vel = {rid: [0.0, 0.0] for rid in self.robot_ids}          # actual (v, w)
        self.cmd = {rid: (0.0, 0.0) for rid in self.robot_ids}
        self.contacts = {rid: 0 for rid in self.robot_ids}
        self._in_contact = {rid: False for rid in self.robot_ids}
        self.distance_m = {rid: 0.0 for rid in self.robot_ids}
        self.sim_time_s = 0.0

    def close(self) -> None:
        pass

    def set_cmd(self, robot_id: str, v: float, w: float) -> None:
        self.cmd[robot_id] = (max(-MAX_V_MPS, min(MAX_V_MPS, v)), max(-MAX_W_RPS, min(MAX_W_RPS, w)))

    def _blocked(self, robot_id: str, x: float, y: float) -> bool:
        r = layout.ROBOT_RADIUS_M
        if layout.blocked(x, y, r):
            return True
        for sx, sy in self.survivors:
            if math.hypot(x - sx, y - sy) < r + layout.SURVIVOR_RADIUS_M:
                return True
        for other in self.robot_ids:
            if other != robot_id:
                ox, oy, _ = self.pose[other]
                if math.hypot(x - ox, y - oy) < 2 * r:
                    return True
        return False

    def step(self, dt: float) -> None:
        for rid in self.robot_ids:
            v, w = self.vel[rid]
            cv, cw = self.cmd[rid]
            v += max(-ACCEL_MPS2 * dt, min(ACCEL_MPS2 * dt, cv - v))
            w += max(-ALPHA_RPS2 * dt, min(ALPHA_RPS2 * dt, cw - w))
            x, y, yaw = self.pose[rid]
            yaw2 = layout.wrap_angle(yaw + w * dt)
            x2 = x + v * math.cos(yaw2) * dt
            y2 = y + v * math.sin(yaw2) * dt
            if self._blocked(rid, x2, y2):
                if not self._in_contact[rid]:
                    self.contacts[rid] += 1
                self._in_contact[rid] = True
                v = 0.0
                self.pose[rid] = [x, y, yaw2]
            else:
                self._in_contact[rid] = False
                self.distance_m[rid] += math.hypot(x2 - x, y2 - y)
                self.pose[rid] = [x2, y2, yaw2]
            self.vel[rid] = [v, w]
        self.sim_time_s += dt

    def get_pose(self, robot_id: str) -> tuple[float, float, float]:
        return tuple(self.pose[robot_id])  # type: ignore[return-value]

    def get_velocity(self, robot_id: str) -> tuple[float, float, float]:
        v, w = self.vel[robot_id]
        yaw = self.pose[robot_id][2]
        return (v * math.cos(yaw), v * math.sin(yaw), w)

    def _discs(self, robot_id: str, with_survivors: bool) -> list[tuple[float, float, float]]:
        discs = [(self.pose[o][0], self.pose[o][1], layout.ROBOT_RADIUS_M) for o in self.robot_ids if o != robot_id]
        if with_survivors:
            discs += [(sx, sy, layout.SURVIVOR_RADIUS_M) for sx, sy in self.survivors]
        return discs

    def scan(self, robot_id: str) -> tuple[np.ndarray, float, float, float]:
        x, y, yaw = self.pose[robot_id]
        angles = yaw + sensors.SCAN_ANGLE_MIN + sensors.SCAN_ANGLE_INCREMENT * np.arange(sensors.SCAN_BEAMS)
        ranges = sensors.cast((x, y), angles, sensors.SCAN_MAX_RANGE_M, self._discs(robot_id, True))
        return ranges.astype(np.float32), sensors.SCAN_ANGLE_MIN, sensors.SCAN_ANGLE_INCREMENT, \
            sensors.SCAN_MAX_RANGE_M

    def detections(self, robot_id: str) -> list[dict]:
        return sensors.detect_survivors(self.get_pose(robot_id), self.survivors, self._discs(robot_id, False))
