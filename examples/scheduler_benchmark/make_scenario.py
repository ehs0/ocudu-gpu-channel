#!/usr/bin/env python3
"""Generate a seeded Sionna scenario for the scheduler benchmark.

One seed fixes everything the radio channel depends on: each UE's
random-waypoint route and speed, and the Sionna PathSolver seed. The bridge's
grid timeline (`run_bridge.py --timeline grid`) then evaluates the route at
`k / update_hz`, so the same seed reproduces the same positions and the same
solved channel at every grid point. Rerunning this script with the same
arguments produces a byte-identical file.

Routes stay in line of sight of the gNB: every point of every leg is checked
against the scene's obstacle boxes (the robot_ring pillars and mast, from the
scene manifest) with a safety margin. A UE behind a pillar loses ~25 dB in one
step, and srsUE does not survive that while connected (docs/one-cell-two-ue-
stability.md); a scheduler comparison needs both UEs attached for the whole run.
The channel still varies between the UEs through distance (path loss) and the
ground/fence reflections.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import pathlib
import random
import sys
from typing import Any, Sequence

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_BASE = PROJECT_ROOT / "examples/configs/sionna/scenarios/robot_ring/robot-ring-walk.json"
SCENES_DIR = PROJECT_ROOT / "examples/configs/sionna/scenes"
GENERATOR_VERSION = 1
UE_IDS = ("ue0", "ue1")
UE_HEIGHT_M = 0.4
# Inflation applied to every obstacle box before the line-of-sight test, so a
# route does not graze an edge where diffraction loss already starts.
OBSTACLE_MARGIN_M = 1.0
SAMPLE_STEP_M = 0.25
MAX_WAYPOINT_TRIES = 2000


class Box:
    """Axis-aligned obstacle box in scene metres."""

    def __init__(self, low: Sequence[float], high: Sequence[float], label: str) -> None:
        self.low = tuple(float(v) for v in low)
        self.high = tuple(float(v) for v in high)
        self.label = label

    def inflated(self, margin: float) -> "Box":
        return Box(
            (self.low[0] - margin, self.low[1] - margin, self.low[2]),
            (self.high[0] + margin, self.high[1] + margin, self.high[2] + margin),
            self.label,
        )

    def segment_hits(self, a: Sequence[float], b: Sequence[float]) -> bool:
        """Slab test: does the segment a->b pass through the box?"""

        t_min, t_max = 0.0, 1.0
        for axis in range(3):
            delta = b[axis] - a[axis]
            if abs(delta) < 1e-12:
                if a[axis] < self.low[axis] or a[axis] > self.high[axis]:
                    return False
                continue
            t0 = (self.low[axis] - a[axis]) / delta
            t1 = (self.high[axis] - a[axis]) / delta
            if t0 > t1:
                t0, t1 = t1, t0
            t_min = max(t_min, t0)
            t_max = min(t_max, t1)
            if t_min > t_max:
                return False
        return True

    def contains_xy(self, point: Sequence[float]) -> bool:
        return self.low[0] <= point[0] <= self.high[0] and self.low[1] <= point[1] <= self.high[1]


def scene_obstacles(scene: str) -> tuple[list[Box], float, dict[str, Any]]:
    """Obstacle boxes, ground half extent and the manifest of a repo scene."""

    manifest_path = SCENES_DIR / scene / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"scene {scene!r} has no manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    boxes: list[Box] = []
    for index, pillar in enumerate(manifest.get("pillars") or []):
        cx, cy = pillar["centre_m"]
        half = float(pillar["side_m"]) / 2.0
        boxes.append(Box((cx - half, cy - half, 0.0), (cx + half, cy + half, float(pillar["height_m"])),
                         f"pillar_{index}"))
    mast = manifest.get("mast")
    if mast:
        mx, my = mast["xy_m"]
        # The mast is thin; treat it as a 0.5 m column so routes keep clear.
        boxes.append(Box((mx - 0.25, my - 0.25, 0.0), (mx + 0.25, my + 0.25, float(mast["height_m"])), "mast"))
    half_extent = float((manifest.get("ground") or {}).get("half_extent_m", 30.0))
    return boxes, half_extent, manifest


class Region:
    """Where a UE may be: on the ground, in line of sight, away from the mast."""

    def __init__(self, gnb: Sequence[float], boxes: list[Box], half_extent: float,
                 min_gnb_distance_m: float, max_gnb_distance_m: float, edge_margin_m: float) -> None:
        self.gnb = tuple(float(v) for v in gnb)
        self.boxes = [box.inflated(OBSTACLE_MARGIN_M) for box in boxes]
        self.limit = half_extent - edge_margin_m
        self.min_d = min_gnb_distance_m
        self.max_d = max_gnb_distance_m
        if self.limit <= 0 or self.min_d >= self.max_d:
            raise ValueError("empty UE region")

    def horizontal_distance(self, point: Sequence[float]) -> float:
        return math.hypot(point[0] - self.gnb[0], point[1] - self.gnb[1])

    def allows(self, point: Sequence[float]) -> bool:
        if abs(point[0]) > self.limit or abs(point[1]) > self.limit:
            return False
        if not self.min_d <= self.horizontal_distance(point) <= self.max_d:
            return False
        for box in self.boxes:
            if box.contains_xy(point):
                return False
            # The gNB sits on top of the mast: its own column never shadows it.
            if box.label != "mast" and box.segment_hits(self.gnb, point):
                return False
        return True

    def leg_allowed(self, a: Sequence[float], b: Sequence[float]) -> bool:
        length = math.dist(a, b)
        steps = max(1, math.ceil(length / SAMPLE_STEP_M))
        return all(
            self.allows(tuple(a[i] + (b[i] - a[i]) * k / steps for i in range(3)))
            for k in range(steps + 1)
        )

    def sample(self, rng: random.Random, max_d: float | None = None) -> tuple[float, float, float]:
        for _ in range(MAX_WAYPOINT_TRIES):
            point = (round(rng.uniform(-self.limit, self.limit), 3),
                     round(rng.uniform(-self.limit, self.limit), 3), UE_HEIGHT_M)
            if self.allows(point) and (max_d is None or self.horizontal_distance(point) <= max_d):
                return point
        raise ValueError("could not sample an allowed waypoint; widen the region")


def random_waypoint_route(rng: random.Random, region: Region, length_m: float,
                          start_max_d: float) -> list[list[float]]:
    """Waypoints whose legs are all in line of sight, covering `length_m`."""

    route = [region.sample(rng, max_d=start_max_d)]
    travelled = 0.0
    while travelled < length_m:
        for _ in range(MAX_WAYPOINT_TRIES):
            candidate = region.sample(rng)
            if math.dist(route[-1], candidate) >= 2.0 and region.leg_allowed(route[-1], candidate):
                break
        else:
            raise ValueError("could not extend the route in line of sight")
        travelled += math.dist(route[-1], candidate)
        route.append(candidate)
    return [list(point) for point in route]


def generate(seed: int, base: dict[str, Any], *, duration_s: float, speed_range: tuple[float, float],
             min_gnb_distance_m: float, max_gnb_distance_m: float, start_max_distance_m: float,
             edge_margin_m: float) -> dict[str, Any]:
    if seed < 0:
        raise ValueError("seed must be non-negative")
    scene = base.get("scene")
    if not isinstance(scene, str):
        raise ValueError("base scenario must name a repo scene")
    nodes = base.get("nodes") or {}
    if "gnb0" not in nodes or any(ue not in nodes for ue in UE_IDS):
        raise ValueError("base scenario must define gnb0, ue0 and ue1")
    gnb = nodes["gnb0"].get("start_m")
    if not (isinstance(gnb, list) and len(gnb) == 3):
        raise ValueError("base gnb0 needs start_m")
    boxes, half_extent, _ = scene_obstacles(scene)
    region = Region(gnb, boxes, half_extent, min_gnb_distance_m, max_gnb_distance_m, edge_margin_m)

    # One stream per purpose, derived from the seed, so adding a UE or a knob
    # later does not shift the draws of the others.
    out = copy.deepcopy(base)
    out["name"] = f"scheduler-benchmark-seed{seed}"
    out["description"] = (
        f"Seeded scheduler-benchmark scenario (generator v{GENERATOR_VERSION}, seed {seed}): "
        "random-waypoint UE routes kept in line of sight of gnb0; PathSolver seed = scenario seed."
    )
    solver = dict(out.get("solver") or {})
    solver["seed"] = seed
    out["solver"] = solver
    routes: dict[str, Any] = {}
    for index, ue in enumerate(UE_IDS):
        rng = random.Random(f"scheduler-benchmark:{seed}:{ue}:mobility")
        speed = round(rng.uniform(*speed_range), 3)
        route = random_waypoint_route(rng, region, speed * duration_s * 1.05, start_max_distance_m)
        node = copy.deepcopy(nodes[ue])
        node["mobility"] = "robot"
        node["route_m"] = route
        node["route_mode"] = "pingpong"
        node["speed_mps"] = speed
        out["nodes"][ue] = node
        routes[ue] = {"speed_mps": speed, "waypoints": len(route),
                      "start_gnb_distance_m": round(region.horizontal_distance(route[0]), 3)}
    out["benchmark"] = {
        "seed": seed,
        "generator_version": GENERATOR_VERSION,
        "duration_s": duration_s,
        "speed_range_mps": list(speed_range),
        "gnb_distance_range_m": [min_gnb_distance_m, max_gnb_distance_m],
        "start_max_gnb_distance_m": start_max_distance_m,
        "obstacle_margin_m": OBSTACLE_MARGIN_M,
        "line_of_sight": "every leg sampled every 0.25 m clears every inflated obstacle box",
        "routes": routes,
    }
    return out


def generate_single_cell(seed: int, scene_id: str, ue_count: int, *, mobility: str, placement: str,
                         duration_s: float, gnb_antennas: int = 1) -> dict[str, Any]:
    """One gNB and `ue_count` UEs in a registered scene (single-cell mode).

    placement `uniform`: each UE (its position, or each waypoint of its walk)
    is drawn uniformly over the scene's allowed region; `hotspot`: around one
    of scene_registry.HOTSPOT_COUNT seeded centres (UE i -> centre i mod
    count). mobility `static`: the UE stays where it was placed;
    `pedestrian`: it walks a ping-pong random-waypoint route at
    scene_registry.PEDESTRIAN_SPEED_MPS, every leg in line of sight.
    Each UE draws from its own stream keyed by (scene, seed, UE id), and the
    hotspot centres from one of their own, so adding UEs leaves ue0..ueK
    exactly where they were. `gnb_antennas` (definitions.GNB_ANTENNA_CONFIGS)
    gives gnb0 a 1 x N array for both directions; it draws nothing, so the
    UEs stand where they stand with any antenna count.
    """

    import definitions as bench
    import scene_registry as reg

    if seed < 0:
        raise ValueError("seed must be non-negative")
    if scene_id not in reg.SCENES:
        raise ValueError(f"unknown scene {scene_id!r}")
    if mobility not in reg.MOBILITY_MODES:
        raise ValueError(f"mobility must be one of {reg.MOBILITY_MODES}")
    if placement not in reg.PLACEMENT_MODES:
        raise ValueError(f"placement must be one of {reg.PLACEMENT_MODES}")
    if not 1 <= ue_count <= bench.MAX_SINGLE_CELL_UES:
        raise ValueError(f"UE count must be 1..{bench.MAX_SINGLE_CELL_UES}")
    if gnb_antennas not in bench.GNB_ANTENNA_CONFIGS:
        raise ValueError(f"gNB antennas must be one of {sorted(bench.GNB_ANTENNA_CONFIGS)}")
    entry = reg.SCENES[scene_id]
    base = reg.base_scenario(scene_id)
    if "gnb0" not in base.get("nodes", {}):
        raise ValueError("base scenario must define gnb0")
    area = reg.region(scene_id, base)
    centres = reg.hotspot_centres(scene_id, seed, area) if placement == "hotspot" else []
    ue_template = next(node for node_id, node in base["nodes"].items() if node_id != "gnb0")
    out = {key: copy.deepcopy(value) for key, value in base.items() if key not in ("nodes", "links", "description")}
    out["name"] = f"single-cell-{scene_id}-{ue_count}ue-{gnb_antennas}ant-{mobility}-{placement}-seed{seed}"
    out["description"] = (f"Single-cell scheduler benchmark scenario (generator v{GENERATOR_VERSION}): one "
                          f"{gnb_antennas}-antenna gnb0 and "
                          f"{ue_count} {mobility} UEs, {placement} placement, in {scene_id}; every position in line "
                          "of sight of the gNB; PathSolver seed = scenario seed.")
    out["solver"] = dict(out.get("solver") or {}, seed=seed)
    gnb = copy.deepcopy(base["nodes"]["gnb0"])
    for key in ("array", "tx_array", "rx_array"):
        gnb.pop(key, None)
    # Sionna's PlanarArray: 1 row x N columns, half-wavelength spacing.
    gnb["array"] = {"rows": 1, "cols": gnb_antennas, "pattern": "iso", "polarization": "V"}
    out["nodes"] = {"gnb0": gnb}
    out["links"] = []
    placements: dict[str, Any] = {}
    for index in range(ue_count):
        ue = f"ue{index}"
        rng = random.Random(entry["rng_key"].format(scene=scene_id, seed=seed, ue=ue, purpose="placement"))
        centre = centres[index % len(centres)] if centres else None

        def draw() -> tuple[float, float, float]:
            return reg.sample_near(rng, area, centre) if centre is not None else area.sample(rng)

        node = {"array": copy.deepcopy(ue_template.get("array") or {"rows": 1, "cols": 1, "pattern": "iso",
                                                                      "polarization": "V"})}
        start = draw()
        record: dict[str, Any] = {"start_m": list(start), "start_gnb_distance_m": round(area.horizontal_distance(start), 3),
                                  "hotspot": index % len(centres) if centres else None}
        if mobility == "static":
            node.update({"mobility": "static", "start_m": list(start)})
        else:
            route = [list(start)]
            travelled = 0.0
            while travelled < reg.PEDESTRIAN_SPEED_MPS * duration_s * 1.05:
                for _ in range(MAX_WAYPOINT_TRIES):
                    candidate = draw()
                    if math.dist(route[-1], candidate) >= 2.0 and area.leg_allowed(route[-1], candidate):
                        break
                else:
                    raise ValueError(f"{ue}: could not extend the walk in line of sight")
                travelled += math.dist(route[-1], candidate)
                route.append(list(candidate))
            node.update({"mobility": "pedestrian", "route_m": route, "route_mode": "pingpong",
                         "speed_mps": reg.PEDESTRIAN_SPEED_MPS})
            record.update({"speed_mps": reg.PEDESTRIAN_SPEED_MPS, "waypoints": len(route)})
        out["nodes"][ue] = node
        placements[ue] = record
    for index in range(ue_count):
        out["links"].append({"from": "gnb0", "to": f"ue{index}", "direction": "downlink", "model": "sionna_rt"})
    for index in range(ue_count):
        out["links"].append({"from": f"ue{index}", "to": "gnb0", "direction": "uplink", "model": "sionna_rt"})
    out["benchmark"] = {
        "mode": "single_cell", "seed": seed, "scene_id": scene_id, "ue_count": ue_count, "gnb_antennas": gnb_antennas,
        "mobility": mobility, "placement": placement, "generator_version": GENERATOR_VERSION,
        "duration_s": duration_s, "region": entry["region"],
        "hotspots": {"centres_m": [list(c) for c in centres], "sigma_m": reg.HOTSPOT_SIGMA_M} if centres else None,
        "pedestrian_speed_mps": reg.PEDESTRIAN_SPEED_MPS if mobility == "pedestrian" else None,
        "line_of_sight": entry["line_of_sight"], "line_of_sight_constrained": True,
        "rng_key": entry["rng_key"], "placements": placements,
    }
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--single-cell-scene", help="single-cell mode: registered scene id (scene_registry.py)")
    parser.add_argument("--ue-count", type=int, help="single-cell mode: number of UEs")
    parser.add_argument("--mobility", default="static", help="single-cell mode: static or pedestrian")
    parser.add_argument("--placement", default="uniform", help="single-cell mode: uniform or hotspot")
    parser.add_argument("--gnb-antennas", type=int, default=1, help="single-cell mode: gNB antennas (1 or 2)")
    parser.add_argument("--base", type=pathlib.Path, default=DEFAULT_BASE,
                        help="scenario supplying the scene, solver, gNB and antenna arrays")
    parser.add_argument("--duration-s", type=float, default=600.0,
                        help="route length is speed x duration (ping-pong afterwards)")
    parser.add_argument("--speed-min-mps", type=float, default=0.5)
    parser.add_argument("--speed-max-mps", type=float, default=1.5)
    parser.add_argument("--min-gnb-distance-m", type=float, default=6.0)
    parser.add_argument("--max-gnb-distance-m", type=float, default=40.0)
    parser.add_argument("--start-max-gnb-distance-m", type=float, default=20.0,
                        help="both UEs attach at scenario time 0; keep them close enough to do so")
    parser.add_argument("--edge-margin-m", type=float, default=3.0)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    if not 0 < args.speed_min_mps <= args.speed_max_mps:
        parser.error("speed range must be positive and ordered")
    if args.duration_s <= 0:
        parser.error("--duration-s must be positive")
    if args.single_cell_scene is not None:
        if args.ue_count is None:
            parser.error("--single-cell-scene needs --ue-count")
        try:
            scenario = generate_single_cell(args.seed, args.single_cell_scene, args.ue_count,
                                            mobility=args.mobility, placement=args.placement,
                                            duration_s=args.duration_s, gnb_antennas=args.gnb_antennas)
        except ValueError as error:
            print(f"scenario generation failed: {error}", file=sys.stderr)
            return 2
        args.out.write_text(json.dumps(scenario, indent=2) + "\n", encoding="utf-8")
        print(f"event=single_cell_scenario seed={args.seed} scene={args.single_cell_scene} "
              f"ues={args.ue_count} gnb_antennas={args.gnb_antennas} mobility={args.mobility} "
              f"placement={args.placement} out={args.out}")
        return 0
    base = json.loads(args.base.read_text(encoding="utf-8"))
    try:
        scenario = generate(
            args.seed, base, duration_s=args.duration_s,
            speed_range=(args.speed_min_mps, args.speed_max_mps),
            min_gnb_distance_m=args.min_gnb_distance_m, max_gnb_distance_m=args.max_gnb_distance_m,
            start_max_distance_m=args.start_max_gnb_distance_m, edge_margin_m=args.edge_margin_m,
        )
    except ValueError as error:
        print(f"scenario generation failed: {error}", file=sys.stderr)
        return 2
    args.out.write_text(json.dumps(scenario, indent=2) + "\n", encoding="utf-8")
    routes = scenario["benchmark"]["routes"]
    print(f"event=scheduler_benchmark_scenario seed={args.seed} out={args.out} "
          + " ".join(f"{ue}_speed={r['speed_mps']} {ue}_waypoints={r['waypoints']}" for ue, r in routes.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
