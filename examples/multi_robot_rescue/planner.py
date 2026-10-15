"""Central planner: deterministic greedy frontier allocation on the fused map (numpy).

Rule-based baseline, not a learned model. Every planning round:

1. Frontier clusters of the fused map (free cells next to unknown).
2. For every robot the server may command (fresh pose, link ONLINE in the
   server's view), path distances over known-free space from its last pose.
3. Greedy assignment, cheapest (robot, cluster) pair first, cost = path length
   - 0.3 m per frontier cell, + `RESERVED_PENALTY_M` when the goal lies within
   `RESERVE_RADIUS_M` of a reserved point (goals already handed out this round,
   the last known positions of robots that are offline, targets of tasks the
   server keeps).

The server turns each assignment into a `mission_task` with a revision and a
ttl; it never sends one to a robot it cannot reach. It only asks for robots
without a live task (commitment, `mission_server.py`), so orders do not
oscillate between rounds.
"""

from __future__ import annotations

import math

import numpy as np

import grid

RESERVE_RADIUS_M = 4.0
RESERVED_PENALTY_M = 8.0
AVOID_RADIUS_M = 1.5


def allocate(state: np.ndarray, robots: dict[str, dict], reserved: list[tuple[float, float]]) \
        -> dict[str, tuple[float, float]]:
    """robots: rid -> {"pose": (x, y), "avoid": [(x, y), ...]}.

    `avoid` are targets this robot refused recently (stuck, unreachable): never
    offered to it again while listed. Returns rid -> target (x, y).
    """

    clusters = grid.frontier_clusters(state)
    if not clusters or not robots:
        return {}
    passable = grid.traversable(state, allow_unknown=False)
    cost = grid.step_cost(state)
    options = []
    for rid, info in sorted(robots.items()):
        cell = grid.cell_of(*info["pose"])
        if not grid.in_grid(*cell):
            continue
        local = passable.copy()
        r, c = cell
        local[max(r - 1, 0):r + 2, max(c - 1, 0):c + 2] = True
        dist = grid.distance_field(local, [cell], cost)
        for index, cluster in enumerate(clusters):
            found = grid.cluster_goal(cluster, dist)
            if found is None:
                continue
            goal_cell, d = found
            goal = grid.centre_of(*goal_cell)
            if any(math.hypot(goal[0] - ax, goal[1] - ay) < AVOID_RADIUS_M for ax, ay in info.get("avoid", ())):
                continue
            cost = d * grid.RESOLUTION_M - 0.3 * len(cluster) * grid.RESOLUTION_M
            options.append((cost, rid, index, goal))
    assignments: dict[str, tuple[float, float]] = {}
    taken: set[int] = set()
    points = list(reserved)
    while True:
        best = None
        for cost, rid, index, goal in options:
            if rid in assignments or index in taken:
                continue
            penalty = RESERVED_PENALTY_M if any(math.hypot(goal[0] - px, goal[1] - py) < RESERVE_RADIUS_M
                                                for px, py in points) else 0.0
            if best is None or cost + penalty < best[0]:
                best = (cost + penalty, rid, index, goal)
        if best is None:
            break
        _, rid, index, goal = best
        assignments[rid] = (round(goal[0], 2), round(goal[1], 2))
        taken.add(index)
        points.append(goal)
    return assignments
