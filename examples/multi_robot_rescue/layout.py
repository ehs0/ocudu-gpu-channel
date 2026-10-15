"""The disaster building: walls, rubble, rooms, robot spawns, survivor presets, radio sites.

The single source of geometry for the world simulators (`kinematic_world.py`,
`isaac_world.py`), the Sionna scene (`../configs/sionna/scenes/multi_robot_rescue/`),
the referee and the dashboard. Robot and central code never read the survivor
positions from here: robots see only their own sensors, the mission server only
what arrives over its UDP socket (`tests/test_rescue_isolation.py`).

Frame ("site"): x east, y north, z up, metres; origin at the building's outer
south-west corner. It is also the Sionna scene frame, so the bridge runs with a
zero position offset.

    y=24 +--------+---------+---------+---------+
         |  N1    |   N2    |   N3    |   N4    |   N4: steel-lined archive
         |        |         |         |  (shadow)  room, the deep-shadow room
    y=14 +--  ----+---  ----+---  ----+---  ----+
  entrance   corridor (rubble narrows it at x~24)    gNB outside the west entrance
    y=10 +--  ----+---  ----+---  ----+---  ----+
         |  S1    |   S2    |   S3    |   S4    |
    y=0  +--------+---------+---------+---------+
         x=0     10        20        30        40
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

BUILDING_X_M = (0.0, 40.0)
BUILDING_Y_M = (0.0, 24.0)
CORRIDOR_Y_M = (10.0, 14.0)
WALL_HEIGHT_M = 3.0
ROOF_THICKNESS_M = 0.3
EXTERIOR_WALL_M = 0.3
INTERIOR_WALL_M = 0.2
SHADOW_WALL_M = 0.3
DOOR_WIDTH_M = 1.2
ENTRANCE_Y_M = (10.5, 13.5)          # open doorway in the west wall
GROUND_HALF_EXTENT_M = 80.0
GROUND_CENTRE_M = (20.0, 12.0)

ROBOT_RADIUS_M = 0.25
ROBOT_ANTENNA_HEIGHT_M = 0.35
SURVIVOR_RADIUS_M = 0.3
ROBOT_IDS = ("robot_1", "robot_2", "robot_3")
# Robot i carries UE node ue{i} of the live gate; ue3 is parked (the gate runs 2 or 4 UEs).
UE_NODE = {robot_id: f"ue{index}" for index, robot_id in enumerate(ROBOT_IDS)}


@dataclass(frozen=True)
class Box:
    """Axis-aligned solid: walls, rubble. Both world simulators and the scene use these."""

    name: str
    x0: float
    y0: float
    x1: float
    y1: float
    z0: float
    z1: float
    material: str

    @property
    def centre(self) -> tuple[float, float]:
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)

    @property
    def size(self) -> tuple[float, float, float]:
        return (self.x1 - self.x0, self.y1 - self.y0, self.z1 - self.z0)


@dataclass(frozen=True)
class Room:
    name: str
    x0: float
    y0: float
    x1: float
    y1: float
    door_x: float          # door centre on the corridor wall

    def contains(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1


@dataclass(frozen=True)
class RadioSite:
    name: str
    gnb_m: tuple[float, float, float]
    parked_ue_m: tuple[float, float, float]
    description: str


# Sionna ITU material per layout material, with slab thickness (m).
MATERIALS = {
    "concrete": ("concrete", EXTERIOR_WALL_M),
    "brick": ("brick", INTERIOR_WALL_M),
    "plasterboard": ("plasterboard", INTERIOR_WALL_M),
    "metal": ("metal", SHADOW_WALL_M),
    "wood": ("wood", 0.1),
}

ROOMS = (
    Room("S1", 0.0, 0.0, 10.0, 10.0, door_x=7.0),
    Room("S2", 10.0, 0.0, 20.0, 10.0, door_x=13.0),
    Room("S3", 20.0, 0.0, 30.0, 10.0, door_x=27.0),
    Room("S4", 30.0, 0.0, 40.0, 10.0, door_x=33.0),
    Room("N1", 0.0, 14.0, 10.0, 24.0, door_x=7.0),
    Room("N2", 10.0, 14.0, 20.0, 24.0, door_x=13.0),
    Room("N3", 20.0, 14.0, 30.0, 24.0, door_x=27.0),
    Room("N4", 30.0, 14.0, 40.0, 24.0, door_x=32.0),
)
SHADOW_ROOM = "N4"


def _wall_x(name, x0, x1, y, thickness, material, gaps=()):
    """A wall along x at y, split around door gaps (centre, width)."""

    boxes, start = [], x0
    for centre, width in sorted(gaps):
        if centre - width / 2 > start:
            boxes.append(Box(f"{name}_{len(boxes)}", start, y - thickness / 2, centre - width / 2,
                             y + thickness / 2, 0.0, WALL_HEIGHT_M, material))
        start = centre + width / 2
    if x1 > start:
        boxes.append(Box(f"{name}_{len(boxes)}", start, y - thickness / 2, x1, y + thickness / 2,
                         0.0, WALL_HEIGHT_M, material))
    return boxes


def _wall_y(name, y0, y1, x, thickness, material, gaps=()):
    boxes, start = [], y0
    for centre, width in sorted(gaps):
        if centre - width / 2 > start:
            boxes.append(Box(f"{name}_{len(boxes)}", x - thickness / 2, start, x + thickness / 2,
                             centre - width / 2, 0.0, WALL_HEIGHT_M, material))
        start = centre + width / 2
    if y1 > start:
        boxes.append(Box(f"{name}_{len(boxes)}", x - thickness / 2, start, x + thickness / 2, y1,
                         0.0, WALL_HEIGHT_M, material))
    return boxes


def _walls() -> list[Box]:
    t = EXTERIOR_WALL_M
    x_min, x_max = BUILDING_X_M
    y_min, y_max = BUILDING_Y_M
    walls: list[Box] = []
    # Exterior: south, north, east; west has the entrance.
    walls += _wall_x("ext_south", x_min, x_max, y_min + t / 2, t, "concrete")
    walls += _wall_x("ext_north", x_min, x_max, y_max - t / 2, t, "concrete")
    walls += _wall_y("ext_east", y_min, y_max, x_max - t / 2, t, "concrete")
    entrance = ((ENTRANCE_Y_M[0] + ENTRANCE_Y_M[1]) / 2, ENTRANCE_Y_M[1] - ENTRANCE_Y_M[0])
    walls += _wall_y("ext_west", y_min, y_max, x_min + t / 2, t, "concrete", gaps=[entrance])
    # Corridor walls with one door per room. N4's stretch is steel-lined.
    south_doors = [(r.door_x, DOOR_WIDTH_M) for r in ROOMS if r.name.startswith("S")]
    north_doors = [(r.door_x, DOOR_WIDTH_M) for r in ROOMS if r.name.startswith("N")]
    walls += _wall_x("corr_south", x_min + t, x_max - t, CORRIDOR_Y_M[0], INTERIOR_WALL_M, "brick",
                     gaps=south_doors)
    n4 = room(SHADOW_ROOM)
    walls += _wall_x("corr_north", x_min + t, n4.x0, CORRIDOR_Y_M[1], INTERIOR_WALL_M, "brick",
                     gaps=[g for g in north_doors if g[0] < n4.x0])
    walls += _wall_x("n4_south", n4.x0, x_max - t, CORRIDOR_Y_M[1], SHADOW_WALL_M, "metal",
                     gaps=[(n4.door_x, DOOR_WIDTH_M)])
    # Partitions between rooms.
    for x in (10.0, 20.0, 30.0):
        material_s = "plasterboard"
        walls += _wall_y(f"part_s{int(x)}", y_min + t, CORRIDOR_Y_M[0], x, INTERIOR_WALL_M, material_s)
        material_n = "metal" if x == n4.x0 else "plasterboard"
        thickness = SHADOW_WALL_M if x == n4.x0 else INTERIOR_WALL_M
        walls += _wall_y(f"part_n{int(x)}", CORRIDOR_Y_M[1], y_max - t, x, thickness, material_n)
    # Steel lining on N4's north and east faces (inside the concrete shell).
    walls.append(Box("n4_lining_north", n4.x0, y_max - t - 0.1, x_max - t, y_max - t, 0.0, WALL_HEIGHT_M,
                     "metal"))
    walls.append(Box("n4_lining_east", x_max - t - 0.1, n4.y0, x_max - t, y_max - t, 0.0, WALL_HEIGHT_M,
                     "metal"))
    return walls


def _rubble() -> list[Box]:
    def r(name, cx, cy, sx, sy, h, material="concrete"):
        return Box(f"rubble_{name}", cx - sx / 2, cy - sy / 2, cx + sx / 2, cy + sy / 2, 0.0, h, material)

    return [
        # Corridor narrows to ~1.4 m at x ~ 24 (partially blocked passage).
        r("corr_a", 24.0, 11.25, 1.6, 2.3, 1.2),
        r("corr_b", 17.0, 13.4, 1.2, 0.8, 0.8, "wood"),
        r("s1", 4.0, 4.0, 2.0, 1.5, 0.9),
        r("s2", 16.0, 6.0, 1.5, 2.5, 1.1, "wood"),
        r("s3", 24.0, 4.0, 3.0, 1.0, 0.7),
        r("s4", 36.0, 7.0, 1.2, 1.2, 1.4),
        r("n1", 5.0, 19.0, 1.8, 1.8, 1.0, "wood"),
        r("n2", 15.0, 20.0, 2.5, 1.2, 0.8),
        r("n3", 25.0, 18.0, 1.4, 2.4, 1.3),
        r("n4", 35.0, 18.5, 2.0, 1.0, 1.0, "metal"),
    ]


def room(name: str) -> Room:
    for item in ROOMS:
        if item.name == name:
            return item
    raise KeyError(name)


WALLS = tuple(_walls())
RUBBLE = tuple(_rubble())
OBSTACLES: tuple[Box, ...] = WALLS + RUBBLE

ROOF = Box("roof", BUILDING_X_M[0], BUILDING_Y_M[0], BUILDING_X_M[1], BUILDING_Y_M[1],
           WALL_HEIGHT_M, WALL_HEIGHT_M + ROOF_THICKNESS_M, "concrete")

# Robots start inside the entrance, facing east down the corridor.
SPAWN_POSES = {
    "robot_1": (1.2, 12.9, 0.0),
    "robot_2": (1.2, 12.0, 0.0),
    "robot_3": (1.2, 11.1, 0.0),
}

RADIO_SITES = {
    "entrance": RadioSite(
        "entrance", gnb_m=(-1.0, 12.0, 2.5), parked_ue_m=(-4.0, 8.0, 1.0),
        description="portable gNB on a tripod just outside the west doorway: the corridor is in line of "
                    "sight, rooms are reached through walls and doors, the steel-lined N4 is the deep "
                    "shadow"),
    "street": RadioSite(
        "street", gnb_m=(-12.0, 12.0, 4.0), parked_ue_m=(-6.0, 8.0, 1.0),
        description="gNB 12 m west of the entrance on the corridor axis: the corridor is in line of "
                    "sight, the far rooms only through several walls"),
}

# Survivor presets. "demo": one survivor deep in the shadow room N4, one in S2.
SURVIVOR_PRESETS = {
    "demo": ((37.5, 22.0), (12.0, 2.5)),
    "near": ((8.0, 2.0), (3.0, 21.5)),
}


def survivor_positions(preset: str, seed: int) -> tuple[tuple[float, float], ...]:
    """Hidden survivor positions: a named preset, or "random" (two rooms drawn by seed)."""

    if preset in SURVIVOR_PRESETS:
        return SURVIVOR_PRESETS[preset]
    if preset != "random":
        raise ValueError(f"unknown survivor preset {preset!r} (known: random, {', '.join(SURVIVOR_PRESETS)})")
    rng = random.Random(seed)
    chosen = rng.sample(ROOMS, 2)
    result = []
    for item in chosen:
        for _ in range(1000):
            x = rng.uniform(item.x0 + 1.0, item.x1 - 1.0)
            y = rng.uniform(item.y0 + 1.0, item.y1 - 1.0)
            if not blocked(x, y, SURVIVOR_RADIUS_M + 0.3):
                result.append((round(x, 2), round(y, 2)))
                break
        else:
            raise RuntimeError(f"no free survivor position in {item.name}")
    return tuple(result)


def blocked(x: float, y: float, radius: float) -> bool:
    """True when a disc of `radius` at (x, y) overlaps any wall or rubble box."""

    for b in OBSTACLES:
        nx = min(max(x, b.x0), b.x1)
        ny = min(max(y, b.y0), b.y1)
        if (x - nx) ** 2 + (y - ny) ** 2 < radius * radius:
            return True
    return False


def inside_building(x: float, y: float) -> bool:
    return BUILDING_X_M[0] < x < BUILDING_X_M[1] and BUILDING_Y_M[0] < y < BUILDING_Y_M[1]


def room_of(x: float, y: float) -> str:
    for item in ROOMS:
        if item.contains(x, y):
            return item.name
    if inside_building(x, y):
        return "corridor"
    return "outside"


def free_cells(resolution_m: float):
    """Truth free-space raster (row = y, col = x) of the building interior, for the coverage metric.

    Returns (rows, cols, set of (row, col)) of cells whose centre is inside the
    building and not inside an obstacle. Only the referee and the analysis use it.
    """

    cols = int(round((BUILDING_X_M[1] - BUILDING_X_M[0]) / resolution_m))
    rows = int(round((BUILDING_Y_M[1] - BUILDING_Y_M[0]) / resolution_m))
    free = set()
    for row in range(rows):
        y = BUILDING_Y_M[0] + (row + 0.5) * resolution_m
        for col in range(cols):
            x = BUILDING_X_M[0] + (col + 0.5) * resolution_m
            if not any(b.x0 <= x <= b.x1 and b.y0 <= y <= b.y1 for b in OBSTACLES):
                free.add((row, col))
    return rows, cols, free


def wrap_angle(angle: float) -> float:
    return (angle + math.pi) % (2 * math.pi) - math.pi
