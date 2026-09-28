#!/usr/bin/env python3
"""Build the indoor room used by the humanoid-robot channel demo.

One invocation writes two things from a single set of dimensions:

  * a Mitsuba scene (``scene.xml`` plus binary PLY meshes) that Sionna RT
    loads through ``run_bridge.py --scene``, and
  * ``room.json``, the same boxes as plain numbers, which the Isaac Sim
    script reads to spawn its colliders.

Authoring the room twice by hand is how the radio scene and the physics
scene drift apart, and a drift of a few centimetres in a 10 m room moves
every reflection. Both consumers therefore read from here.

The room is a square concrete shell containing:

  * a long metal table along the north wall -- the dominant reflector, and
    the reason the channel changes as much as it does when a robot walks,
  * a glass window in the west wall, which unlike the concrete around it
    lets energy out and reflects what stays with a different coefficient,
  * a small square wooden table at the centre.

Walls are modelled as slabs rather than single-sided planes so that a ray
meets a surface with an outward normal from either side, which is what the
ITU radio materials expect. The west wall is cut into four slabs around the
window opening, and the glass fills the hole: a window painted onto a solid
wall would still be backed by concrete and would behave like concrete.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import struct
from typing import Sequence

# Radio materials, keyed by the bsdf id used in scene.xml. The thickness is
# what an ITU material needs to compute transmission through the surface, so
# it tracks the slab geometry rather than being a free parameter: the glass
# pane is thin, the structure around it is not.
MATERIALS = {
    "concrete": ("concrete", 0.2),
    "wood": ("wood", 0.05),
    "metal": ("metal", 0.02),
    "glass": ("glass", 0.01),
}


def box(center: Sequence[float], size: Sequence[float]):
    """Axis-aligned box as (vertices, triangle faces) wound outward."""

    (cx, cy, cz), (sx, sy, sz) = center, size
    x0, x1 = cx - sx / 2.0, cx + sx / 2.0
    y0, y1 = cy - sy / 2.0, cy + sy / 2.0
    z0, z1 = cz - sz / 2.0, cz + sz / 2.0
    vertices = [
        [x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
        [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1],
    ]
    quads = [
        [0, 3, 2, 1],  # -z
        [4, 5, 6, 7],  # +z
        [0, 1, 5, 4],  # -y
        [3, 7, 6, 2],  # +y
        [0, 4, 7, 3],  # -x
        [1, 2, 6, 5],  # +x
    ]
    # Mitsuba's PLY loader rejects anything but triangles, so each face is
    # split here rather than left as a quad. The winding of both halves
    # follows the quad, which keeps the outward normal.
    faces = []
    for a, b, c, d in quads:
        faces += [[a, b, c], [a, c, d]]
    return vertices, faces


def write_ply(path: pathlib.Path, vertices, faces) -> None:
    """Binary little-endian PLY, the format the other Sionna scenes use."""

    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        f"element vertex {len(vertices)}\n"
        "property float x\nproperty float y\nproperty float z\n"
        f"element face {len(faces)}\n"
        "property list uchar int vertex_indices\n"
        "end_header\n"
    ).encode("ascii")
    body = bytearray(header)
    for vertex in vertices:
        body += struct.pack("<3f", *vertex)
    for face in faces:
        body += struct.pack("<B", len(face)) + struct.pack(f"<{len(face)}i", *face)
    path.write_bytes(body)


def room_boxes(args) -> list[dict]:
    """Every solid in the scene, as centre/size/material triples.

    Object ids matter beyond bookkeeping: run_bridge's `surface_kind()`
    classifies a shape by the prefix of its id, which is what colours it in
    the web viewer.
    """

    span, height = args.room_span_m, args.room_height_m
    thickness = args.wall_thickness_m
    # Shell slabs sit entirely outside the clear interior, so the usable
    # volume is exactly span x span x height regardless of wall thickness.
    outer = span + 2.0 * thickness
    half = span / 2.0 + thickness / 2.0
    interior = span / 2.0

    boxes = [
        {"id": "floor", "material": "concrete",
         "center": [0.0, 0.0, -thickness / 2.0], "size": [outer, outer, thickness]},
        {"id": "building_wall_south", "material": "concrete",
         "center": [0.0, -half, height / 2.0], "size": [outer, thickness, height]},
        {"id": "building_wall_north", "material": "concrete",
         "center": [0.0, half, height / 2.0], "size": [outer, thickness, height]},
    ]
    # The east and west walls are not listed here: both are cut into pieces
    # further down, around the door and the window respectively.

    # The room is open to the sky unless asked otherwise. A roof is the single
    # biggest source of multipath in a closed hall -- every ray that leaves a
    # robot upward comes back -- so removing it both clears the view from
    # above and leaves a far simpler channel to reason about.
    if args.ceiling:
        boxes.insert(1, {
            "id": "building_ceiling", "material": "concrete",
            "center": [0.0, 0.0, height + thickness / 2.0],
            "size": [outer, outer, thickness],
        })

    # West wall, cut into four concrete slabs around the window opening.
    width = args.window_width_m
    sill, head = args.window_sill_m, args.window_sill_m + args.window_height_m
    flank = (span - width) / 2.0
    boxes += [
        {"id": "building_wall_west_south", "material": "concrete",
         "center": [-half, -(width / 2.0 + flank / 2.0), height / 2.0],
         "size": [thickness, flank, height]},
        {"id": "building_wall_west_north", "material": "concrete",
         "center": [-half, width / 2.0 + flank / 2.0, height / 2.0],
         "size": [thickness, flank, height]},
        {"id": "building_wall_west_below", "material": "concrete",
         "center": [-half, 0.0, sill / 2.0], "size": [thickness, width, sill]},
        {"id": "building_wall_west_above", "material": "concrete",
         "center": [-half, 0.0, (head + height) / 2.0],
         "size": [thickness, width, height - head]},
        {"id": "building_window", "material": "glass",
         "center": [-half, 0.0, (sill + head) / 2.0],
         "size": [args.glass_thickness_m, width, head - sill]},
    ]

    # East wall -- the one facing the window -- carries a wooden door in its
    # southern corner. Like the window, the opening is cut out of the
    # concrete rather than painted on it, so the door leaf is the only thing
    # a ray meets there.
    door_width, door_height = args.door_m
    door_y0 = -interior + args.door_corner_offset_m
    door_y1 = door_y0 + door_width
    door_center_y = (door_y0 + door_y1) / 2.0
    south_flank = args.door_corner_offset_m
    north_flank = interior - door_y1
    boxes += [
        {"id": "building_wall_east_south", "material": "concrete",
         "center": [half, -interior + south_flank / 2.0, height / 2.0],
         "size": [thickness, south_flank, height]},
        {"id": "building_wall_east_north", "material": "concrete",
         "center": [half, door_y1 + north_flank / 2.0, height / 2.0],
         "size": [thickness, north_flank, height]},
        {"id": "building_wall_east_above", "material": "concrete",
         "center": [half, door_center_y, (door_height + height) / 2.0],
         "size": [thickness, door_width, height - door_height]},
        {"id": "wood_door", "material": "wood",
         "center": [half, door_center_y, door_height / 2.0],
         "size": [args.door_thickness_m, door_width, door_height]},
    ]

    # A long wooden bench against the north wall and its twin opposite, so
    # the room is furnished symmetrically along that axis.
    length, depth, table_height = args.wall_table_m
    for name, sign in (("north", 1.0), ("south", -1.0)):
        boxes.append({
            "id": f"wood_wall_table_{name}", "material": "wood",
            "center": [0.0, sign * (interior - depth / 2.0), table_height / 2.0],
            "size": [length, depth, table_height],
        })

    # Two identical square tables side by side at the centre, the pair
    # centred on the room. Solid blocks rather than tabletops on legs: a slab
    # is the shape the ray tracer handles most predictably.
    side, wood_height = args.centre_table_m
    gap = args.centre_table_gap_m
    for name, sign in (("west", -1.0), ("east", 1.0)):
        boxes.append({
            "id": f"wood_centre_table_{name}", "material": "wood",
            "center": [args.centre_pair_center_m[0] + sign * (side + gap) / 2.0,
                       args.centre_pair_center_m[1], wood_height / 2.0],
            "size": [side, side, wood_height],
        })
    return boxes


def write_scene(directory: pathlib.Path, boxes: list[dict]) -> None:
    meshes = directory / "meshes"
    meshes.mkdir(parents=True, exist_ok=True)

    used = {item["material"] for item in boxes}
    lines = ["<?xml version='1.0' encoding='utf-8'?>", '<scene version="2.1.0">']
    for material_id, (itu_type, thickness) in MATERIALS.items():
        if material_id not in used:
            continue
        lines += [
            f'  <bsdf type="itu-radio-material" id="{material_id}">',
            f'    <string name="type" value="{itu_type}" />',
            f'    <float name="thickness" value="{thickness}" />',
            "  </bsdf>",
        ]
    for item in boxes:
        vertices, faces = box(item["center"], item["size"])
        write_ply(meshes / f"{item['id']}.ply", vertices, faces)
        lines += [
            f'  <shape type="ply" id="mesh-{item["id"]}">',
            f'    <string name="filename" value="meshes/{item["id"]}.ply" />',
            '    <boolean name="face_normals" value="true" />',
            f'    <ref id="{item["material"]}" name="bsdf" />',
            "  </shape>",
        ]
    lines.append("</scene>")
    (directory / "scene.xml").write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, required=True,
                        help="scene directory to write (scene.xml, meshes/, room.json)")
    # An ordinary 10 m room with room-sized furniture.
    parser.add_argument("--room-span-m", type=float, default=10.0,
                        help="clear interior side length of the square room")
    parser.add_argument("--room-height-m", type=float, default=3.0)
    parser.add_argument("--wall-thickness-m", type=float, default=0.2)
    parser.add_argument("--ceiling", action=argparse.BooleanOptionalAction, default=False,
                        help="roof the room; off by default so it can be seen into")
    parser.add_argument("--window-width-m", type=float, default=4.0)
    parser.add_argument("--window-sill-m", type=float, default=0.9)
    parser.add_argument("--window-height-m", type=float, default=1.4)
    parser.add_argument("--glass-thickness-m", type=float, default=0.02)
    parser.add_argument("--door-m", type=float, nargs=2, default=[0.9, 2.1],
                        metavar=("WIDTH", "HEIGHT"),
                        help="wooden door in the east wall, facing the window")
    parser.add_argument("--door-corner-offset-m", type=float, default=0.2,
                        help="reveal between the south corner and the door")
    parser.add_argument("--door-thickness-m", type=float, default=0.05)
    parser.add_argument("--wall-table-m", type=float, nargs=3, default=[6.0, 0.8, 0.9],
                        metavar=("LENGTH", "DEPTH", "HEIGHT"),
                        help="long table, placed against the north and south walls")
    parser.add_argument("--centre-table-m", type=float, nargs=2, default=[2.0, 0.75],
                        metavar=("SIDE", "HEIGHT"),
                        help="each of the two square tables at the centre")
    parser.add_argument("--centre-table-gap-m", type=float, default=0.2,
                        help="clearance between the two centre tables")
    parser.add_argument("--centre-pair-center-m", type=float, nargs=2, default=[0.0, 0.0])
    args = parser.parse_args()

    boxes = room_boxes(args)
    args.out.mkdir(parents=True, exist_ok=True)
    write_scene(args.out, boxes)

    # The physics side reads this instead of parsing PLY. Interior bounds are
    # included so the Isaac script can clamp robot spawns without recomputing
    # the wall arithmetic.
    (args.out / "room.json").write_text(json.dumps({
        "coordinate_system": "Sionna XYZ, metres, Z up, origin at room centre floor",
        "room_span_m": args.room_span_m,
        "room_height_m": args.room_height_m,
        "interior_bounds_m": {
            "x": [-args.room_span_m / 2.0, args.room_span_m / 2.0],
            "y": [-args.room_span_m / 2.0, args.room_span_m / 2.0],
            "z": [0.0, args.room_height_m],
        },
        "boxes": boxes,
    }, indent=2) + "\n")

    print(f"wrote {len(boxes)} solids to {args.out}")
    for item in boxes:
        print(f"  {item['id']:28} {item['material']:9} "
              f"center={[round(v, 2) for v in item['center']]} "
              f"size={[round(v, 2) for v in item['size']]}")


if __name__ == "__main__":
    main()
