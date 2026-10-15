"""2-D occupancy grids, map tiles, frontiers and wavefront planning (numpy).

Used by robot.py (its own map, built from its own scans) and by
mission_server.py (the fused map, built only from delivered tiles). Cells are
`RESOLUTION_M` squares over the building's bounding box in the site frame;
row = y, column = x.

Tiles: the grid is cut into `TILE_CELLS` x `TILE_CELLS` blocks. A robot sends a
whole tile (tri-state, 2 bits per cell) whenever it changed, so every tile
message is self-contained: there is no base revision to lose, and the newest
tile per (robot, tile) is the robot's current view of it.
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np

import layout
import messages

RESOLUTION_M = 0.2
TILE_CELLS = 16
X0, Y0 = layout.BUILDING_X_M[0], layout.BUILDING_Y_M[0]
COLS = int(round((layout.BUILDING_X_M[1] - X0) / RESOLUTION_M))
ROWS = int(round((layout.BUILDING_Y_M[1] - Y0) / RESOLUTION_M))
TILES_X = math.ceil(COLS / TILE_CELLS)
TILES_Y = math.ceil(ROWS / TILE_CELLS)

UNKNOWN, FREE, OCCUPIED = messages.UNKNOWN, messages.FREE, messages.OCCUPIED
L_FREE, L_OCC, L_MIN, L_MAX = -0.4, 0.85, -2.0, 3.5
L_FREE_THRESHOLD, L_OCC_THRESHOLD = -0.3, 0.6


def cell_of(x: float, y: float) -> tuple[int, int]:
    return int((y - Y0) / RESOLUTION_M), int((x - X0) / RESOLUTION_M)


def centre_of(row: int, col: int) -> tuple[float, float]:
    return X0 + (col + 0.5) * RESOLUTION_M, Y0 + (row + 0.5) * RESOLUTION_M


def in_grid(row: int, col: int) -> bool:
    return 0 <= row < ROWS and 0 <= col < COLS


class OccupancyGrid:
    """Log-odds grid of one robot, updated from its own 2-D scans."""

    def __init__(self):
        self.logodds = np.zeros((ROWS, COLS), dtype=np.float32)
        self.sent_state = np.zeros((ROWS, COLS), dtype=np.uint8)   # tri-state last emitted per tile

    def state(self) -> np.ndarray:
        out = np.full((ROWS, COLS), UNKNOWN, dtype=np.uint8)
        out[self.logodds < L_FREE_THRESHOLD] = FREE
        out[self.logodds > L_OCC_THRESHOLD] = OCCUPIED
        return out

    def integrate_scan(self, pose: tuple[float, float, float], ranges: np.ndarray, angle_min: float,
                       angle_increment: float, max_range: float) -> None:
        x, y, yaw = pose
        angles = yaw + angle_min + angle_increment * np.arange(len(ranges))
        hit = ranges < max_range - 1e-3
        reach = np.minimum(ranges, max_range)
        # Free cells: samples every half cell along each beam, stopping short of the hit.
        steps = np.arange(0.0, max_range, RESOLUTION_M / 2)
        dist = steps[None, :]
        # Free space stops one cell short of a hit, so beams grazing a wall do not erode it.
        stop = np.where(hit, reach - RESOLUTION_M, reach)
        mask = dist < stop[:, None]
        px = x + np.cos(angles)[:, None] * dist
        py = y + np.sin(angles)[:, None] * dist
        rows = ((py - Y0) / RESOLUTION_M).astype(np.int64)[mask]
        cols = ((px - X0) / RESOLUTION_M).astype(np.int64)[mask]
        keep = (rows >= 0) & (rows < ROWS) & (cols >= 0) & (cols < COLS)
        free_idx = np.unique(rows[keep] * COLS + cols[keep])
        flat = self.logodds.reshape(-1)
        flat[free_idx] += L_FREE
        # Hit cells.
        hx = x + np.cos(angles[hit]) * ranges[hit]
        hy = y + np.sin(angles[hit]) * ranges[hit]
        hr = ((hy - Y0) / RESOLUTION_M).astype(np.int64)
        hc = ((hx - X0) / RESOLUTION_M).astype(np.int64)
        keep = (hr >= 0) & (hr < ROWS) & (hc >= 0) & (hc < COLS)
        occ_idx = np.unique(hr[keep] * COLS + hc[keep])
        flat[occ_idx] += L_OCC - L_FREE * np.isin(occ_idx, free_idx)
        np.clip(self.logodds, L_MIN, L_MAX, out=self.logodds)

    def changed_tiles(self) -> list[tuple[int, int]]:
        """Tiles whose tri-state differs from what was last emitted (and marks them emitted)."""

        current = self.state()
        diff = current != self.sent_state
        if not diff.any():
            return []
        tiles = []
        for ty in range(TILES_Y):
            for tx in range(TILES_X):
                block = diff[ty * TILE_CELLS:(ty + 1) * TILE_CELLS, tx * TILE_CELLS:(tx + 1) * TILE_CELLS]
                if block.any():
                    tiles.append((tx, ty))
        self.sent_state = current
        return tiles


def tile_cells(state: np.ndarray, tx: int, ty: int) -> list[int]:
    """Cells of tile (tx, ty), row-major, padded with UNKNOWN at the grid edge."""

    block = np.full((TILE_CELLS, TILE_CELLS), UNKNOWN, dtype=np.uint8)
    part = state[ty * TILE_CELLS:(ty + 1) * TILE_CELLS, tx * TILE_CELLS:(tx + 1) * TILE_CELLS]
    block[:part.shape[0], :part.shape[1]] = part
    return block.reshape(-1).tolist()


def tile_payload(state: np.ndarray, tx: int, ty: int) -> dict:
    return {"tile": [tx, ty], "size": TILE_CELLS, "res_m": RESOLUTION_M,
            "cells": messages.pack_cells(tile_cells(state, tx, ty))}


def apply_tile(state: np.ndarray, payload: dict) -> None:
    tx, ty = payload["tile"]
    size = payload["size"]
    if size != TILE_CELLS or not (0 <= tx < TILES_X and 0 <= ty < TILES_Y):
        raise ValueError(f"tile {tx},{ty} size {size} does not fit this grid")
    cells = np.array(messages.unpack_cells(payload["cells"], size * size), dtype=np.uint8).reshape(size, size)
    target = state[ty * size:(ty + 1) * size, tx * size:(tx + 1) * size]
    target[:, :] = cells[:target.shape[0], :target.shape[1]]


def pack_state(state: np.ndarray) -> str:
    return messages.pack_cells(state.reshape(-1).tolist())


def unpack_state(text: str) -> np.ndarray:
    return np.array(messages.unpack_cells(text, ROWS * COLS), dtype=np.uint8).reshape(ROWS, COLS)


# --------------------------------------------------------------------------- planning

def _shift(a: np.ndarray, dr: int, dc: int, fill) -> np.ndarray:
    out = np.full_like(a, fill)
    rs = slice(max(dr, 0), a.shape[0] + min(dr, 0))
    rd = slice(max(-dr, 0), a.shape[0] + min(-dr, 0))
    cs = slice(max(dc, 0), a.shape[1] + min(dc, 0))
    cd = slice(max(-dc, 0), a.shape[1] + min(-dc, 0))
    out[rs, cs] = a[rd, cd]
    return out


def inflate(occupied: np.ndarray, radius_cells: int) -> np.ndarray:
    out = occupied.copy()
    for dr in range(-radius_cells, radius_cells + 1):
        for dc in range(-radius_cells, radius_cells + 1):
            if dr * dr + dc * dc <= radius_cells * radius_cells and (dr or dc):
                out |= _shift(occupied, dr, dc, False)
    return out


# Cells within HARD_INFLATE_CELLS of an obstacle are impassable; the next band up to
# SOFT_INFLATE_CELLS costs SOFT_COST per step, so paths keep to the middle of a door
# (1.2 m = 6 cells: 4 passable, 2 of them cheap) without closing it.
HARD_INFLATE_CELLS = 1
SOFT_INFLATE_CELLS = 2
SOFT_COST = 4.0


def traversable(state: np.ndarray, *, allow_unknown: bool) -> np.ndarray:
    ok = ~inflate(state == OCCUPIED, HARD_INFLATE_CELLS)
    if not allow_unknown:
        ok &= state == FREE
    return ok


def step_cost(state: np.ndarray) -> np.ndarray:
    cost = np.ones(state.shape, dtype=np.float32)
    cost[inflate(state == OCCUPIED, SOFT_INFLATE_CELLS)] = SOFT_COST
    return cost


NEIGHBOURS = ((-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
              (-1, -1, math.sqrt(2)), (-1, 1, math.sqrt(2)), (1, -1, math.sqrt(2)), (1, 1, math.sqrt(2)))


def distance_field(passable: np.ndarray, sources: list[tuple[int, int]], cost: np.ndarray | None = None,
                   max_iterations: int = 4000) -> np.ndarray:
    """8-connected path cost (cells x `cost` of the entered cell) from `sources` over `passable`.

    Wavefront relaxation in numpy until nothing changes. Unreachable cells are
    inf. Sources need not be passable themselves (a robot standing next to a
    wall in the inflated band still plans out of it).
    """

    dist = np.full(passable.shape, np.inf, dtype=np.float32)
    for row, col in sources:
        if in_grid(row, col):
            dist[row, col] = 0.0
    weight = np.ones(passable.shape, dtype=np.float32) if cost is None else cost
    for _ in range(max_iterations):
        best = dist.copy()
        for dr, dc, step in NEIGHBOURS:
            np.minimum(best, _shift(dist, dr, dc, np.inf) + step * weight, out=best)
        best[~passable] = np.where(dist[~passable] == 0.0, 0.0, np.inf)
        if np.array_equal(best, dist):
            break
        dist = best
    return dist


def descend(dist: np.ndarray, start: tuple[int, int], steps: int) -> list[tuple[int, int]]:
    """Follow the distance field downhill from `start` for up to `steps` cells."""

    path = [start]
    row, col = start
    for _ in range(steps):
        best = (dist[row, col], row, col)
        for dr, dc, _cost in NEIGHBOURS:
            r, c = row + dr, col + dc
            if in_grid(r, c) and dist[r, c] < best[0]:
                best = (dist[r, c], r, c)
        if (best[1], best[2]) == (row, col):
            break
        row, col = best[1], best[2]
        path.append((row, col))
        if dist[row, col] == 0.0:
            break
    return path


def frontier_mask(state: np.ndarray) -> np.ndarray:
    """Free cells with a 4-neighbour of unknown, inside the building."""

    unknown = state == UNKNOWN
    near_unknown = (_shift(unknown, 1, 0, False) | _shift(unknown, -1, 0, False)
                    | _shift(unknown, 0, 1, False) | _shift(unknown, 0, -1, False))
    return (state == FREE) & near_unknown


def frontier_clusters(state: np.ndarray, min_cells: int = 4) -> list[list[tuple[int, int]]]:
    mask = frontier_mask(state)
    cells = set(zip(*np.nonzero(mask)))
    clusters = []
    while cells:
        seed = cells.pop()
        cluster = [seed]
        queue = deque([seed])
        while queue:
            row, col = queue.popleft()
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    n = (row + dr, col + dc)
                    if n in cells:
                        cells.remove(n)
                        cluster.append(n)
                        queue.append(n)
        if len(cluster) >= min_cells:
            clusters.append([(int(r), int(c)) for r, c in cluster])
    return clusters


def cluster_goal(cluster: list[tuple[int, int]], dist: np.ndarray) -> tuple[tuple[int, int], float] | None:
    """The reachable cluster cell nearest the cluster centroid, and its path distance (cells)."""

    reachable = [(r, c) for r, c in cluster if np.isfinite(dist[r, c])]
    if not reachable:
        return None
    mr = sum(r for r, _ in cluster) / len(cluster)
    mc = sum(c for _, c in cluster) / len(cluster)
    goal = min(reachable, key=lambda rc: (rc[0] - mr) ** 2 + (rc[1] - mc) ** 2)
    return goal, float(dist[goal])


def known_fraction(state: np.ndarray, truth_free: np.ndarray) -> float:
    """Share of truly free cells that `state` knows (free or occupied). Referee/analysis only."""

    total = int(truth_free.sum())
    return float(((state != UNKNOWN) & truth_free).sum()) / total if total else 0.0


def truth_free_mask() -> np.ndarray:
    """Truth free-space raster on this grid (from layout.py). Referee/analysis only."""

    rows, cols, free = layout.free_cells(RESOLUTION_M)
    assert (rows, cols) == (ROWS, COLS)
    mask = np.zeros((ROWS, COLS), dtype=bool)
    for row, col in free:
        mask[row, col] = True
    return mask
