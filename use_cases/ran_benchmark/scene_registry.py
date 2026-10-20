#!/usr/bin/env python3
"""Scenes the single-cell scheduler benchmark can run, and where UEs may stand.

Two scenes are offered: the SUTD campus (OpenStreetMap buildings, roads, grass
and water with ITU radio materials) with ONE gNB on the Building 2 rooftop,
and simple_blocks_2x2 (four box buildings of different heights in concrete,
glass and wood, apps/sionna_bridge/build_blocks_scene.py) with ONE gNB on a
mast at the crossroads.
The robot_ring arena and the two-gNB SUTD layout stay available to the
two-cell mode and the other gates, but not here.

A scene is offered only when its files are installed (scene XML, meshes,
manifest) and it has a placement adapter: the rule for where a UE may be and
which straight segments to the gNB are in line of sight (building prisms).

UE placement is `uniform` (every allowed point equally likely) or `hotspot`
(UEs clustered around HOTSPOT_COUNT seeded centres); UE type is `static` (no
movement) or `pedestrian` (random-waypoint walk at PEDESTRIAN_SPEED_MPS,
around its own hotspot when placement is hotspot).

Whether a (scene, UE count) combination may be STARTED from the web UI is a
separate fact: it needs a recorded end-to-end run in scene_validation.json,
with either gNB antenna count (an entry without `gnb_antennas` is a 1-antenna
run). The antenna count changes the cell, not where the UEs stand or whether
they attach (both counts attached 4 UEs on sutd_1gnb, g22 and g25).
Combinations without that evidence are listed as unvalidated, and the
controller refuses them unless the operator started it with
--allow-unvalidated to produce the evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import pathlib
import random
import sys
from typing import Any, Sequence

HERE = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
SCENES_DIR = PROJECT_ROOT / "use_cases/configs/sionna/scenes"
SCENARIOS_DIR = PROJECT_ROOT / "use_cases/configs/sionna/scenarios"
VALIDATION_FILE = HERE / "scene_validation.json"
sys.path.insert(0, str(HERE))

import make_scenario  # noqa: E402

MOBILITY_MODES = ("static", "pedestrian")
PLACEMENT_MODES = ("uniform", "hotspot")
UE_HEIGHT_DEFAULT_M = 1.5
PEDESTRIAN_SPEED_MPS = 1.4       # run_bridge.PEDESTRIAN_SPEED_MPS, the walking speed used elsewhere
HOTSPOT_COUNT = 2                # fixed, so a UE's hotspot does not depend on N
HOTSPOT_SIGMA_M = 8.0            # per-axis spread of UEs (and walk waypoints) around a centre
HOTSPOT_CENTRE_BAND_M = (25.0, 65.0)  # centre distance from the gNB


def _point_in_polygon(x: float, y: float, polygon: Sequence[Sequence[float]]) -> bool:
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _distance_to_polygon_edge(x: float, y: float, polygon: Sequence[Sequence[float]]) -> float:
    best = math.inf
    for i in range(len(polygon)):
        ax, ay = polygon[i - 1][0], polygon[i - 1][1]
        bx, by = polygon[i][0], polygon[i][1]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2))
        best = min(best, math.hypot(x - (ax + t * dx), y - (ay + t * dy)))
    return best


class PrismRegion:
    """Building prisms from an OSM manifest: footprint polygon x height.

    A UE point is allowed when it lies within the distance band of the gNB,
    inside the ground, at least `margin` from every footprint, and the 3D
    segment gNB -> UE passes above every building it crosses (sampled every
    `step` metres; the gNB's own roof is crossed above its height).
    """

    def __init__(self, gnb: Sequence[float], buildings: list[dict[str, Any]], half_extent: float,
                 min_d: float, max_d: float, edge_margin: float, margin: float = 3.0,
                 ue_height: float = UE_HEIGHT_DEFAULT_M, step: float = 1.0) -> None:
        self.gnb = tuple(float(v) for v in gnb)
        self.buildings = [(b["footprint_m"], float(b["height_m"])) for b in buildings if b.get("footprint_m")]
        self.limit = half_extent - edge_margin
        self.min_d, self.max_d = min_d, max_d
        self.margin = margin
        self.ue_height = ue_height
        self.step = step
        if self.limit <= 0 or self.min_d >= self.max_d:
            raise ValueError("empty UE region")

    def horizontal_distance(self, point: Sequence[float]) -> float:
        return math.hypot(point[0] - self.gnb[0], point[1] - self.gnb[1])

    def _clear_of_buildings(self, point: Sequence[float]) -> bool:
        for footprint, _ in self.buildings:
            if _point_in_polygon(point[0], point[1], footprint):
                return False
            if _distance_to_polygon_edge(point[0], point[1], footprint) < self.margin:
                return False
        return True

    def _line_of_sight(self, point: Sequence[float]) -> bool:
        length = math.dist(self.gnb[:2], point[:2])
        steps = max(1, math.ceil(length / self.step))
        for k in range(1, steps):
            t = k / steps
            x = self.gnb[0] + (point[0] - self.gnb[0]) * t
            y = self.gnb[1] + (point[1] - self.gnb[1]) * t
            z = self.gnb[2] + (point[2] - self.gnb[2]) * t
            for footprint, height in self.buildings:
                if z < height + 0.5 and _point_in_polygon(x, y, footprint):
                    return False
        return True

    def allows(self, point: Sequence[float]) -> bool:
        if abs(point[0]) > self.limit or abs(point[1]) > self.limit:
            return False
        if not self.min_d <= self.horizontal_distance(point) <= self.max_d:
            return False
        return self._clear_of_buildings(point) and self._line_of_sight(point)

    def leg_allowed(self, a: Sequence[float], b: Sequence[float]) -> bool:
        length = math.dist(a, b)
        steps = max(1, math.ceil(length / 1.0))
        return all(self.allows(tuple(a[i] + (b[i] - a[i]) * k / steps for i in range(3)))
                   for k in range(steps + 1))

    def sample(self, rng: random.Random, max_d: float | None = None) -> tuple[float, float, float]:
        reach = self.max_d if max_d is None else min(self.max_d, max_d)
        for _ in range(make_scenario.MAX_WAYPOINT_TRIES):
            point = (round(self.gnb[0] + rng.uniform(-reach, reach), 3),
                     round(self.gnb[1] + rng.uniform(-reach, reach), 3), self.ue_height)
            if self.allows(point) and self.horizontal_distance(point) <= reach:
                return point
        raise ValueError("could not sample an allowed UE position; widen the region")


# Each entry: the asset directory under use_cases/configs/sionna/scenes, the
# scenario whose gNB/solver/antenna settings the generated scenario starts
# from, the placement adapter's parameters and the UE counts it offers.
SCENES: dict[str, dict[str, Any]] = {
    "sutd_1gnb": {
        "label": "SUTD campus, 1 gNB (Building 2 rooftop)",
        "asset": "sionna_SUTD_test",
        "base_scenario": "sutd/sionna-multi-ue-sutd.json",
        "adapter": "prisms",
        "region": {"min_gnb_distance_m": 15.0, "max_gnb_distance_m": 80.0, "edge_margin_m": 10.0},
        "ue_counts": [1, 2, 4],
        "ue_height_m": UE_HEIGHT_DEFAULT_M,
        "rng_key": "scheduler-benchmark:{scene}:{seed}:{ue}:{purpose}",
        "line_of_sight": "3D segment gNB->UE passes 0.5 m above every OSM building prism it crosses; "
                         "UEs stay 3 m from every footprint",
    },
    "simple_blocks_2x2": {
        "label": "Simple 2x2 blocks (concrete/glass/wood), 1 gNB (25 m mast)",
        "asset": "simple_blocks_2x2",
        "base_scenario": "simple_blocks/sionna-1gnb-simple-blocks.json",
        "adapter": "prisms",
        "region": {"min_gnb_distance_m": 15.0, "max_gnb_distance_m": 80.0, "edge_margin_m": 10.0},
        "ue_counts": [1, 2, 4],
        "ue_height_m": UE_HEIGHT_DEFAULT_M,
        "rng_key": "scheduler-benchmark:{scene}:{seed}:{ue}:{purpose}",
        "line_of_sight": "3D segment gNB->UE passes 0.5 m above every building prism it crosses; "
                         "UEs stay 3 m from every footprint",
    },
}


def scene_files(scene_id: str) -> dict[str, pathlib.Path]:
    entry = SCENES[scene_id]
    root = SCENES_DIR / entry["asset"]
    return {"scene_xml": root / "scene.xml", "manifest": root / "manifest.json", "meshes": root / "meshes",
            "base_scenario": SCENARIOS_DIR / entry["base_scenario"]}


def scene_asset_sha256(scene_id: str) -> str:
    """SHA-256 over every file of the scene directory, path-ordered."""

    root = SCENES_DIR / SCENES[scene_id]["asset"] if scene_id in SCENES else SCENES_DIR / scene_id
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def missing_files(scene_id: str) -> list[str]:
    missing = []
    for label, path in scene_files(scene_id).items():
        if label == "meshes":
            if not path.is_dir() or not any(path.iterdir()):
                missing.append(str(path))
        elif not path.is_file():
            missing.append(str(path))
    return missing


def load_validation() -> dict[str, Any]:
    try:
        return json.loads(VALIDATION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"validated": []}


def validated_ue_counts(scene_id: str, validation: dict[str, Any] | None = None,
                        gnb_antennas: int | None = None) -> list[int]:
    """UE counts with evidence for the scene; for one antenna count, or any."""

    validation = load_validation() if validation is None else validation
    return sorted({int(item["ue_count"]) for item in validation.get("validated", [])
                   if item.get("scene_id") == scene_id
                   and (gnb_antennas is None or int(item.get("gnb_antennas", 1)) == gnb_antennas)})


def base_scenario(scene_id: str) -> dict[str, Any]:
    return json.loads(scene_files(scene_id)["base_scenario"].read_text(encoding="utf-8"))


def region(scene_id: str, base: dict[str, Any] | None = None) -> PrismRegion:
    """The scene's placement region around its gNB."""

    entry = SCENES[scene_id]
    base = base_scenario(scene_id) if base is None else base
    gnb = base["nodes"]["gnb0"]["start_m"]
    params = entry["region"]
    manifest = json.loads(scene_files(scene_id)["manifest"].read_text(encoding="utf-8"))
    half_extent = float((manifest.get("ground") or {}).get("half_extent_m", manifest.get("half_extent_m", 300.0)))
    return PrismRegion(gnb, manifest.get("buildings") or [], half_extent, params["min_gnb_distance_m"],
                       params["max_gnb_distance_m"], params["edge_margin_m"], ue_height=entry["ue_height_m"])


def hotspot_centres(scene_id: str, seed: int, area: PrismRegion) -> list[tuple[float, float, float]]:
    """HOTSPOT_COUNT allowed centres, from their own stream (independent of N)."""

    rng = random.Random(SCENES[scene_id]["rng_key"].format(scene=scene_id, seed=seed, ue="all", purpose="hotspots"))
    low, high = HOTSPOT_CENTRE_BAND_M
    centres: list[tuple[float, float, float]] = []
    for _ in range(make_scenario.MAX_WAYPOINT_TRIES):
        point = area.sample(rng, max_d=high)
        if area.horizontal_distance(point) >= low and all(math.dist(point, c) > 2 * HOTSPOT_SIGMA_M for c in centres):
            centres.append(point)
            if len(centres) == HOTSPOT_COUNT:
                return centres
    raise ValueError("could not place the hotspot centres")


def sample_near(rng: random.Random, area: PrismRegion, centre: Sequence[float]) -> tuple[float, float, float]:
    """An allowed point drawn from a Gaussian (HOTSPOT_SIGMA_M per axis) around `centre`."""

    for _ in range(make_scenario.MAX_WAYPOINT_TRIES):
        point = (round(rng.gauss(centre[0], HOTSPOT_SIGMA_M), 3), round(rng.gauss(centre[1], HOTSPOT_SIGMA_M), 3),
                 area.ue_height)
        if area.allows(point):
            return point
    raise ValueError("could not place a UE near its hotspot")


def catalog(allow_unvalidated: bool = False) -> dict[str, Any]:
    """What /api/benchmark/options offers: installed scenes with adapters.

    `startable_ue_counts` is what the UI may enable, for either gNB antenna
    count; it is the validated set, or every count with records when the
    operator allowed unvalidated runs.
    """

    import definitions as bench  # local: keep this module importable alone

    validation = load_validation()
    scenes = []
    for scene_id, entry in SCENES.items():
        missing = missing_files(scene_id)
        offered = [n for n in entry["ue_counts"] if n <= bench.MAX_SINGLE_CELL_UES]
        counts = [n for n in validated_ue_counts(scene_id, validation) if n in offered]
        scenes.append({
            "id": scene_id,
            "label": entry["label"],
            "installed": not missing,
            "missing_files": missing,
            "validated": bool(counts) and not missing,
            "validated_ue_counts": counts,
            "ue_counts": offered,
            "startable_ue_counts": ([] if missing else (offered if allow_unvalidated else counts)),
            "mobility_modes": list(MOBILITY_MODES),
            "placement_modes": list(PLACEMENT_MODES),
            "pedestrian_speed_mps": PEDESTRIAN_SPEED_MPS,
            "hotspots": {"count": HOTSPOT_COUNT, "sigma_m": HOTSPOT_SIGMA_M},
            "line_of_sight": entry["line_of_sight"],
            "evidence": [item for item in validation.get("validated", []) if item.get("scene_id") == scene_id],
        })
    return {"scenes": scenes, "allow_unvalidated": allow_unvalidated}


if __name__ == "__main__":
    print(json.dumps(catalog(), indent=2))
