"""2-D ray casting against the layout, and the survivor perception MOCK (numpy).

`cast` intersects rays with the layout's boxes (walls, rubble) and with discs
(other robots, survivors). The kinematic world uses it as its LiDAR; both
worlds use it for the perception mock's line-of-sight test.

**Perception mock.** `detect_survivors` is a geometric stand-in for a camera
detector: a survivor is reported when its centre is inside the camera's field
of view and range and the line of sight is clear. It reads the survivors'
true positions (the world process owns them) and returns only bearing, range
and a confidence — never an identity — to the robot. It is a development
mock; no perception-accuracy claim rests on it.
"""

from __future__ import annotations

import math

import numpy as np

import layout

SCAN_BEAMS = 180
SCAN_MAX_RANGE_M = 8.0
SCAN_ANGLE_MIN = -math.pi
SCAN_ANGLE_INCREMENT = 2 * math.pi / SCAN_BEAMS
CAMERA_FOV_RAD = math.radians(90.0)
CAMERA_RANGE_M = 5.0

_BOXES = np.array([[b.x0, b.y0, b.x1, b.y1] for b in layout.OBSTACLES], dtype=np.float64)


def cast(origin: tuple[float, float], angles: np.ndarray, max_range: float,
         discs: list[tuple[float, float, float]] = ()) -> np.ndarray:
    """Distance along each ray to the first box or disc, capped at max_range."""

    ox, oy = origin
    dx = np.cos(angles)[:, None]
    dy = np.sin(angles)[:, None]
    with np.errstate(divide="ignore", invalid="ignore"):
        inv_x = np.where(np.abs(dx) > 1e-12, 1.0 / dx, np.inf)
        inv_y = np.where(np.abs(dy) > 1e-12, 1.0 / dy, np.inf)
        tx1 = (_BOXES[None, :, 0] - ox) * inv_x
        tx2 = (_BOXES[None, :, 2] - ox) * inv_x
        ty1 = (_BOXES[None, :, 1] - oy) * inv_y
        ty2 = (_BOXES[None, :, 3] - oy) * inv_y
    # Per-axis entry/exit; a ray parallel to an axis is inside that slab for all t, or never.
    par_x = ~np.isfinite(inv_x)
    inside_x = (ox >= _BOXES[None, :, 0]) & (ox <= _BOXES[None, :, 2])
    near_x = np.where(par_x, np.where(inside_x, -np.inf, np.inf), np.minimum(tx1, tx2))
    far_x = np.where(par_x, np.where(inside_x, np.inf, -np.inf), np.maximum(tx1, tx2))
    par_y = ~np.isfinite(inv_y)
    inside_y = (oy >= _BOXES[None, :, 1]) & (oy <= _BOXES[None, :, 3])
    near_y = np.where(par_y, np.where(inside_y, -np.inf, np.inf), np.minimum(ty1, ty2))
    far_y = np.where(par_y, np.where(inside_y, np.inf, -np.inf), np.maximum(ty1, ty2))
    t_near = np.maximum(near_x, near_y)
    t_far = np.minimum(far_x, far_y)
    valid = (t_far >= np.maximum(t_near, 0.0)) & (t_far >= 0)
    t_hit = np.where(valid, np.maximum(t_near, 0.0), np.inf)
    best = t_hit.min(axis=1)
    for cx, cy, radius in discs:
        fx, fy = ox - cx, oy - cy
        b = fx * dx[:, 0] + fy * dy[:, 0]
        c = fx * fx + fy * fy - radius * radius
        disc = b * b - c
        root = np.sqrt(np.maximum(disc, 0.0))
        t = -b - root
        t = np.where(t < 0, -b + root, t)
        t = np.where((disc >= 0) & (t >= 0), t, np.inf)
        best = np.minimum(best, t)
    return np.minimum(best, max_range)


def line_of_sight(a: tuple[float, float], b: tuple[float, float], discs=()) -> bool:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return True
    hit = cast(a, np.array([math.atan2(dy, dx)]), length + 1.0, discs)[0]
    return hit >= length - 1e-6


def detect_survivors(pose: tuple[float, float, float], survivors: list[tuple[float, float]],
                     discs_without_survivors: list[tuple[float, float, float]]) -> list[dict]:
    """Perception MOCK: bearing/range of each visible survivor (no identity)."""

    x, y, yaw = pose
    out = []
    for sx, sy in survivors:
        dx, dy = sx - x, sy - y
        distance = math.hypot(dx, dy)
        if distance > CAMERA_RANGE_M:
            continue
        bearing = layout.wrap_angle(math.atan2(dy, dx) - yaw)
        if abs(bearing) > CAMERA_FOV_RAD / 2:
            continue
        # Aim at the near edge of the survivor's disc; other robots can occlude.
        edge = max(distance - layout.SURVIVOR_RADIUS_M, 0.0)
        target = (x + dx / distance * edge, y + dy / distance * edge) if distance > 0 else (sx, sy)
        if not line_of_sight((x, y), target, discs_without_survivors):
            continue
        confidence = max(0.5, 0.98 - 0.08 * distance)
        out.append({"bearing_rad": round(bearing, 4), "range_m": round(distance, 3),
                    "confidence": round(confidence, 3)})
    return out
