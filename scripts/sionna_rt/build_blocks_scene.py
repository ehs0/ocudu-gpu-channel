#!/usr/bin/env python3
"""Build a synthetic Sionna RT scene: four box buildings on a 2x2 grid.

A deliberately simple counterpart to the OpenStreetMap scenes: four square
buildings of different heights and ITU radio materials (concrete, glass,
wood) around one crossroads, on a flat ground plane. The output has the same
layout and manifest fields as build_osm_scene.py, so the scheduler
benchmark's placement adapter (footprint prisms) works on it unchanged:

    examples/configs/sionna/scenes/<name>/
        scene.xml          Mitsuba scene, ITU radio materials, relative meshes
        meshes/*.ply       ground and the four extruded buildings
        manifest.json      buildings (footprint, height, material) in metres

Coordinates are local east/north/up metres; the crossroads is the origin.

Example:

    python3 scripts/sionna_rt/build_blocks_scene.py --name simple_blocks_2x2
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Sequence

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from build_osm_scene import (  # noqa: E402
    GROUND_MATERIAL,
    extrusion,
    ground_plane,
    write_ply,
    write_scene_xml,
)

REPO_ROOT = HERE.parents[1]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "examples/configs/sionna/scenes"

HALF_EXTENT_M = 100.0
BLOCK_SIZE_M = 20.0      # square footprint side
STREET_WIDTH_M = 20.0    # gap between neighbouring blocks
# (id, quadrant sign x, sign y, height, ITU material)
BUILDINGS: tuple[tuple[str, int, int, float, str], ...] = (
    ("building_ne_concrete", 1, 1, 30.0, "concrete"),
    ("building_nw_glass", -1, 1, 22.0, "glass"),
    ("building_sw_wood", -1, -1, 9.0, "wood"),
    ("building_se_concrete", 1, -1, 15.0, "concrete"),
)


def footprint(sx: int, sy: int) -> list[tuple[float, float]]:
    """Counter-clockwise square of one block, a half street from each axis."""

    near, far = STREET_WIDTH_M / 2, STREET_WIDTH_M / 2 + BLOCK_SIZE_M
    xs = sorted((sx * near, sx * far))
    ys = sorted((sy * near, sy * far))
    return [(xs[0], ys[0]), (xs[1], ys[0]), (xs[1], ys[1]), (xs[0], ys[1])]


def build(output: pathlib.Path, name: str) -> dict[str, Any]:
    meshes = output / "meshes"
    meshes.mkdir(parents=True, exist_ok=True)
    shapes: list[tuple[str, str, str]] = []
    manifest_buildings = []
    for building_id, sx, sy, height, material in BUILDINGS:
        ring = footprint(sx, sy)
        vertices, faces = extrusion(ring, 0.0, height)
        write_ply(meshes / f"{building_id}.ply", vertices, faces)
        shapes.append((building_id, building_id, material))
        cx = sum(x for x, _ in ring) / len(ring)
        cy = sum(y for _, y in ring) / len(ring)
        manifest_buildings.append({
            "id": building_id,
            "material": material,
            "height_m": height,
            "footprint_area_m2": BLOCK_SIZE_M * BLOCK_SIZE_M,
            "centroid_m": [cx, cy],
            "roof_center_m": [cx, cy, height],
            "footprint_m": [list(p) for p in ring],
        })
    vertices, faces = ground_plane(HALF_EXTENT_M)
    write_ply(meshes / "ground.ply", vertices, faces)
    shapes.append(("ground", "ground", GROUND_MATERIAL))
    write_scene_xml(output / "scene.xml", shapes)

    material_counts: dict[str, int] = {}
    for entry in manifest_buildings:
        material_counts[entry["material"]] = material_counts.get(entry["material"], 0) + 1
    ground_extent = float(vertices[1][0])
    return {
        "name": name,
        "generator": "scripts/sionna_rt/build_blocks_scene.py",
        "building_materials": dict(sorted(material_counts.items())),
        "half_extent_m": HALF_EXTENT_M,
        "coordinate_system": "local east/north/up metres, crossroads at the origin",
        "block_size_m": BLOCK_SIZE_M,
        "street_width_m": STREET_WIDTH_M,
        "ground": {"material": GROUND_MATERIAL, "half_extent_m": ground_extent},
        "buildings": manifest_buildings,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="simple_blocks_2x2")
    parser.add_argument("--output-root", type=pathlib.Path, default=DEFAULT_OUTPUT_ROOT)
    args = parser.parse_args(argv)
    output = args.output_root / args.name
    output.mkdir(parents=True, exist_ok=True)
    manifest = build(output, args.name)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {output} ({len(manifest['buildings'])} buildings: {manifest['building_materials']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
