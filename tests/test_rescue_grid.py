"""Grids, tiles, frontiers and planning; central map fusion keeps the newest view per tile."""

import pathlib
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "multi_robot_rescue"))

import grid  # noqa: E402
import layout  # noqa: E402
import planner  # noqa: E402
import sensors  # noqa: E402
from mission_server import FusedMap  # noqa: E402


def test_tile_round_trip():
    rng = np.random.default_rng(1)
    state = rng.integers(0, 3, size=(grid.ROWS, grid.COLS), dtype=np.uint8)
    out = np.zeros_like(state)
    for ty in range(grid.TILES_Y):
        for tx in range(grid.TILES_X):
            grid.apply_tile(out, grid.tile_payload(state, tx, ty))
    assert np.array_equal(out, state)
    assert np.array_equal(grid.unpack_state(grid.pack_state(state)), state)


def test_scan_builds_free_and_occupied_cells():
    pose = layout.SPAWN_POSES["robot_2"]
    angles = pose[2] + sensors.SCAN_ANGLE_MIN + sensors.SCAN_ANGLE_INCREMENT * np.arange(sensors.SCAN_BEAMS)
    ranges = sensors.cast(pose[:2], angles, sensors.SCAN_MAX_RANGE_M).astype(np.float32)
    occ = grid.OccupancyGrid()
    for _ in range(3):
        occ.integrate_scan(pose, ranges, sensors.SCAN_ANGLE_MIN, sensors.SCAN_ANGLE_INCREMENT,
                           sensors.SCAN_MAX_RANGE_M)
    state = occ.state()
    assert state[grid.cell_of(pose[0] + 2.0, pose[1])] == grid.FREE          # down the corridor
    north_wall = grid.cell_of(pose[0] + 1.0, layout.CORRIDOR_Y_M[1] - 0.05)   # the wall face the beams hit
    assert state[north_wall] == grid.OCCUPIED
    assert grid.frontier_clusters(state)
    tiles = occ.changed_tiles()
    assert tiles and occ.changed_tiles() == []


def test_scan_matches_brute_force_marching():
    x, y = 15.0, 12.0
    angles = np.linspace(-np.pi, np.pi, 37)
    fast = sensors.cast((x, y), angles, 8.0)
    for angle, hit in zip(angles, fast):
        d = 0.0
        while d < 8.0 and not layout.blocked(x + np.cos(angle) * d, y + np.sin(angle) * d, 1e-6):
            d += 0.005
        assert abs(min(d, 8.0) - hit) < 0.02


def test_distance_field_and_descent_reach_the_goal():
    truth = np.where(grid.truth_free_mask(), grid.FREE, grid.OCCUPIED).astype(np.uint8)
    passable = grid.traversable(truth, allow_unknown=False)
    start, goal = grid.cell_of(2.0, 12.0), grid.cell_of(13.0, 4.0)        # corridor -> room S2, via its door
    dist = grid.distance_field(passable, [goal])
    assert np.isfinite(dist[start])
    path = grid.descend(dist, start, 1000)
    assert path[-1] == goal
    assert all(passable[c] or c == start for c in path)


def test_shadow_room_is_reachable_through_its_door():
    truth = np.where(grid.truth_free_mask(), grid.FREE, grid.OCCUPIED).astype(np.uint8)
    passable = grid.traversable(truth, allow_unknown=False)
    dist = grid.distance_field(passable, [grid.cell_of(*layout.SURVIVOR_PRESETS["demo"][0])])
    assert np.isfinite(dist[grid.cell_of(2.0, 12.0)])


def test_fused_map_keeps_the_newest_tile():
    fm = FusedMap()
    free = np.full((grid.ROWS, grid.COLS), grid.FREE, dtype=np.uint8)
    occ = np.full((grid.ROWS, grid.COLS), grid.OCCUPIED, dtype=np.uint8)
    assert fm.apply("robot_1", grid.tile_payload(occ, 0, 0), captured_at_ms=200)
    assert not fm.apply("robot_1", grid.tile_payload(free, 0, 0), captured_at_ms=100)   # late, older view
    assert fm.fused()[0, 0] == grid.OCCUPIED
    assert fm.apply("robot_2", grid.tile_payload(free, 0, 0), captured_at_ms=300)       # newer, other robot
    assert fm.fused()[0, 0] == grid.FREE
    unknown = np.zeros((grid.ROWS, grid.COLS), dtype=np.uint8)
    assert fm.apply("robot_3", grid.tile_payload(unknown, 0, 0), captured_at_ms=400)
    assert fm.fused()[0, 0] == grid.FREE                                                 # unknown never erases


@pytest.mark.parametrize("avoid", [False, True])
def test_planner_spreads_robots_and_honours_refusals(avoid):
    # A known corridor walled on both sides, unknown beyond both ends: two frontiers.
    state = np.zeros((grid.ROWS, grid.COLS), dtype=np.uint8)
    rows = slice(grid.cell_of(0, 10.0)[0], grid.cell_of(0, 14.0)[0])
    state[rows, 5:grid.COLS - 5] = grid.OCCUPIED
    state[grid.cell_of(0, 10.6)[0]:grid.cell_of(0, 13.4)[0], 5:grid.COLS - 5] = grid.FREE
    robots = {"robot_1": {"pose": (5.0, 12.0)}, "robot_2": {"pose": (5.0, 12.5)}}
    first = planner.allocate(state, robots, reserved=[])
    assert set(first) == {"robot_1", "robot_2"}
    a, b = first["robot_1"], first["robot_2"]
    assert np.hypot(a[0] - b[0], a[1] - b[1]) > 20.0     # opposite ends
    if avoid:
        robots["robot_1"]["avoid"] = [a]
        again = planner.allocate(state, robots, reserved=[])
        assert "robot_1" not in again or np.hypot(again["robot_1"][0] - a[0], again["robot_1"][1] - a[1]) >= 1.5
