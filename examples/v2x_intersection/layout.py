"""The blind urban intersection, in one place.

Every other part of the demo reads its geometry from here: the SUMO network
(`sumo_world.py` writes the plain node/edge XML from these numbers), the
Sionna RT scene (`examples/configs/sionna/scenes/v2x_intersection/
build_v2x_intersection.py`), the Sionna scenarios (gNB and parked-UE
positions), the vehicle controller (conflict point, stop line) and the local
line-of-sight sensor (building footprints). So the road the vehicles drive on
and the buildings the rays bounce off are the same geometry by construction.

Frame ("scene frame"): metres, intersection centre at the origin, x east,
y north, z up. SUMO uses the same frame: the network is built with
normalisation disabled, so a SUMO (x, y) is a scene (x, y) and only the
antenna height is added for Sionna (`tests/test_v2x_layout.py` checks this
against the generated network).

    y
    ^          |  N  |
    |   NW     |     |     NE
    |  block   |     |    block
    |----------+-----+----------   <- B drives west on the north lane (y = +1.75)
    |     W               E
    |----------+-----+----------
    |   SW     |     |     SE      <- the SE block hides B from A
    |  block   |  ^  |    block
    |          |  A  |             <- A drives north on the east lane (x = +1.75)
    +---------------------------> x

Vehicle A (ego, carries the live UE `ue0`) approaches from the south, vehicle
B (scripted) from the east. Neither sees the other until both are within a
few metres of the corner of the SE block.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

LANE_WIDTH_M = 3.5
# Building faces sit this far from the road centre lines (road half-width
# 3.5 m plus a 4.5 m sidewalk).
SETBACK_M = 8.0
BLOCK_EXTENT_M = 70.0           # blocks reach from SETBACK_M to this, on each axis
BUILDING_HEIGHT_M = 18.0
GROUND_HALF_EXTENT_M = 200.0

# Lane centre lines (SUMO places a one-lane edge to the right of its axis).
A_LANE_X_M = LANE_WIDTH_M / 2.0     # northbound lane
B_LANE_Y_M = LANE_WIDTH_M / 2.0     # westbound lane
CONFLICT_POINT_M = (A_LANE_X_M, B_LANE_Y_M)

# Where each vehicle's own approach must stop to keep clear of the crossing
# lane: the building line, measured along its own approach from the
# conflict point.
STOP_LINE_GAP_M = SETBACK_M + B_LANE_Y_M          # 9.75 m for A (and B)

# SUMO network nodes. The dead-end nodes sit beyond the start positions so a
# vehicle is inserted on a straight lane.
NET_NODES_M = {
    "C": (0.0, 0.0),
    "S": (0.0, -150.0),
    "N": (0.0, 120.0),
    "E": (150.0, 0.0),
    "W": (-120.0, 0.0),
}
ROUTE_A = ("S2C", "C2N")
ROUTE_B = ("E2C", "C2W")

# Default start positions (lane centre) and speeds.
A_START_M = (A_LANE_X_M, -100.0)
B_START_M = (108.0, B_LANE_Y_M)
CRUISE_SPEED_MPS = 13.89            # 50 km/h
# A point on B's approach lane. When A has line of sight to it and B is not
# visible, A's local sensor reports the crossing approach as clear.
CROSS_APPROACH_PROBE_M = (CONFLICT_POINT_M[0] + 30.0, B_LANE_Y_M)
VEHICLE_LENGTH_M = 4.5
VEHICLE_WIDTH_M = 1.8
ANTENNA_HEIGHT_M = 1.5


@dataclass(frozen=True)
class Block:
    """An axis-aligned building footprint, scene metres."""

    name: str
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    height_m: float = BUILDING_HEIGHT_M

    @property
    def centre(self) -> tuple[float, float]:
        return (0.5 * (self.x_min + self.x_max), 0.5 * (self.y_min + self.y_max))

    @property
    def size(self) -> tuple[float, float]:
        return (self.x_max - self.x_min, self.y_max - self.y_min)


def _blocks() -> tuple[Block, ...]:
    s, e = SETBACK_M, BLOCK_EXTENT_M
    return (
        Block("block_ne", s, e, s, e),
        Block("block_nw", -e, -s, s, e),
        Block("block_sw", -e, -s, -e, -s),
        Block("block_se", s, e, -e, -s),
    )


BLOCKS = _blocks()


# Radio placements, one per scenario. Each names the gNB antenna and where the
# second UE the two-UE gate requires is parked (static, in line of sight of
# the gNB, carries no application traffic).
@dataclass(frozen=True)
class RadioSite:
    name: str
    gnb_m: tuple[float, float, float]
    parked_ue_m: tuple[float, float, float]
    description: str


RADIO_SITES = {
    # On A's street axis south of the blocks: A is in line of sight for the
    # whole approach, the street canyon only adds reflections.
    "los": RadioSite(
        "los",
        gnb_m=(-5.0, -160.0, 10.0),
        parked_ue_m=(-5.0, -130.0, ANTENNA_HEIGHT_M),
        description="gNB on A's street axis 160 m south: A in line of sight throughout",
    ),
    # South-west of the blocks: A is in line of sight at the start and loses it
    # behind the SW block about 50 m before the conflict point, i.e. while the
    # warning about B matters.
    "blocked": RadioSite(
        "blocked",
        gnb_m=(-40.0, -140.0, 10.0),
        parked_ue_m=(-40.0, -110.0, ANTENNA_HEIGHT_M),
        description="gNB south-west: the SW block shadows A for the last ~50 m before the crossing",
    ),
}


def segment_hits_block(p: tuple[float, float], q: tuple[float, float], block: Block) -> bool:
    """True if the 2-D segment p-q passes through the block footprint.

    Liang-Barsky clipping against the rectangle; touching an edge counts as
    a hit only if the segment enters the interior.
    """

    x0, y0 = p
    dx, dy = q[0] - x0, q[1] - y0
    t0, t1 = 0.0, 1.0
    for pk, qk in (
        (-dx, x0 - block.x_min),
        (dx, block.x_max - x0),
        (-dy, y0 - block.y_min),
        (dy, block.y_max - y0),
    ):
        if pk == 0.0:
            if qk <= 0.0:
                return False
            continue
        t = qk / pk
        if pk < 0.0:
            if t > t1:
                return False
            t0 = max(t0, t)
        else:
            if t < t0:
                return False
            t1 = min(t1, t)
    return t1 - t0 > 1e-9


def line_of_sight(p: tuple[float, float], q: tuple[float, float]) -> bool:
    """Ground-level line of sight between two points (no building in between)."""

    return not any(segment_hits_block(p, q, block) for block in BLOCKS)


def distance(p: tuple[float, float], q: tuple[float, float]) -> float:
    return math.hypot(q[0] - p[0], q[1] - p[1])
