#!/usr/bin/env python3
"""Drive a 2-gNB/2-UE OCUDU channel from Sionna RT.

The two gNBs remain fixed. UE0 and UE1 follow configurable bounded horizontal
trajectories. At a low control-plane cadence, Sionna RT
recomputes all bidirectional gNB/UE paths plus UE-to-UE crosstalk, and this
process atomically sends the resulting ten TDL profiles to the existing C++
broker. OCUDU is not
imported or modified; it only sees the impaired IQ emitted by the broker.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import signal
import struct
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from channel_adapter import (  # type: ignore
        Ray,
        Tap,
        ZmqControlClient,
        channel_status,
        control_link_id,
        make_profile_swap,
        rays_to_taps,
    )
else:
    from .channel_adapter import (
        Ray,
        Tap,
        ZmqControlClient,
        channel_status,
        control_link_id,
        make_profile_swap,
        rays_to_taps,
    )


MODEL_ID = "sionna_rt"
NODE_IDS = ("gnb0", "gnb1", "ue0", "ue1")
DOWNLINK_LINKS = tuple(
    (source, destination)
    for source in ("gnb0", "gnb1")
    for destination in ("ue0", "ue1")
)
UPLINK_LINKS = tuple(
    (source, destination)
    for source in ("ue0", "ue1")
    for destination in ("gnb0", "gnb1")
)
CROSSTALK_LINKS = (("ue0", "ue1"), ("ue1", "ue0"))
LINKS = DOWNLINK_LINKS + UPLINK_LINKS + CROSSTALK_LINKS
ONE_GNB_ONE_UE_NODE_IDS = ("gnb0", "ue0")
ONE_GNB_ONE_UE_DOWNLINK_LINKS = (("gnb0", "ue0"),)
ONE_GNB_ONE_UE_UPLINK_LINKS = (("ue0", "gnb0"),)
ONE_GNB_ONE_UE_CROSSTALK_LINKS: tuple[tuple[str, str], ...] = ()
ONE_GNB_ONE_UE_LINKS = (
    ONE_GNB_ONE_UE_DOWNLINK_LINKS + ONE_GNB_ONE_UE_UPLINK_LINKS
)

# The tracked OCUDU example uses NR band 3 with DL NR-ARFCN 368500. Its
# downlink center is 1842.5 MHz and the paired uplink is 95 MHz lower. Keep
# these as bridge defaults rather than changing OCUDU's radio configuration.
DEFAULT_DOWNLINK_FREQUENCY_HZ = 1_842_500_000.0
DEFAULT_UPLINK_FREQUENCY_HZ = 1_747_500_000.0
DEFAULT_ROAD_WIDTH_M = 14.0
DEFAULT_ROUTE_X_M = (-82.0, 82.0)
DEFAULT_GNB_HEIGHT_M = 60.0
CAR_SPEED_MPS = 50.0 / 3.6
PEDESTRIAN_SPEED_MPS = 1.4


@dataclass(frozen=True)
class Motion:
    start: tuple[float, float, float]
    velocity: tuple[float, float, float]
    x_bounds: tuple[float, float] | None = None
    mobility: str = "static"

    def position_at(self, elapsed_seconds: float) -> tuple[float, float, float]:
        position = tuple(
            origin + elapsed_seconds * speed
            for origin, speed in zip(self.start, self.velocity)
        )
        if self.x_bounds is None or self.velocity[0] == 0.0:
            return position
        low, high = self.x_bounds
        span = high - low
        phase = ((self.start[0] - low) + elapsed_seconds * self.velocity[0]) % (
            2.0 * span
        )
        x = low + phase if phase <= span else high - (phase - span)
        return (x, position[1], position[2])

    def velocity_at(self, elapsed_seconds: float) -> tuple[float, float, float]:
        if self.x_bounds is None or self.velocity[0] == 0.0:
            return self.velocity
        low, high = self.x_bounds
        span = high - low
        phase = ((self.start[0] - low) + elapsed_seconds * self.velocity[0]) % (
            2.0 * span
        )
        direction = 1.0 if phase < span else -1.0
        return (self.velocity[0] * direction, self.velocity[1], self.velocity[2])


DEFAULT_MOTION = {
    "gnb0": Motion(
        (32.5, 10.5, DEFAULT_GNB_HEIGHT_M),
        (0.0, 0.0, 0.0),
        mobility="fixed",
    ),
    "gnb1": Motion(
        (32.5, 45.0, DEFAULT_GNB_HEIGHT_M),
        (0.0, 0.0, 0.0),
        mobility="fixed",
    ),
    "ue0": Motion(
        (-70.0, 0.0, 1.5),
        (CAR_SPEED_MPS, 0.0, 0.0),
        DEFAULT_ROUTE_X_M,
        "car",
    ),
    "ue1": Motion(
        (20.0, 8.0, 1.5),
        (-PEDESTRIAN_SPEED_MPS, 0.0, 0.0),
        DEFAULT_ROUTE_X_M,
        "pedestrian",
    ),
}


def link_layout(
    layout: str,
) -> tuple[
    tuple[str, ...],
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str], ...],
]:
    if layout == "1x1":
        return (
            ONE_GNB_ONE_UE_NODE_IDS,
            ONE_GNB_ONE_UE_DOWNLINK_LINKS,
            ONE_GNB_ONE_UE_UPLINK_LINKS,
            ONE_GNB_ONE_UE_CROSSTALK_LINKS,
            ONE_GNB_ONE_UE_LINKS,
        )
    if layout == "2x2":
        return NODE_IDS, DOWNLINK_LINKS, UPLINK_LINKS, CROSSTALK_LINKS, LINKS
    raise ValueError(f"unsupported link layout: {layout}")


def configured_motion(args: argparse.Namespace) -> dict[str, Motion]:
    motion = {
        "gnb0": Motion(
            (
                DEFAULT_MOTION["gnb0"].start[0],
                DEFAULT_MOTION["gnb0"].start[1],
                args.gnb_height_m,
            ),
            (0.0, 0.0, 0.0),
            mobility="fixed",
        ),
        "gnb1": Motion(
            (
                DEFAULT_MOTION["gnb1"].start[0],
                DEFAULT_MOTION["gnb1"].start[1],
                args.gnb_height_m,
            ),
            (0.0, 0.0, 0.0),
            mobility="fixed",
        ),
        "ue0": Motion(
            args.ue0_start, args.ue0_velocity, args.ue0_route_x, "car"
        ),
        "ue1": Motion(
            args.ue1_start, args.ue1_velocity, args.ue1_route_x, "pedestrian"
        ),
    }
    node_ids, *_ = link_layout(args.layout)
    return {node_id: motion[node_id] for node_id in node_ids}


def scenario_environment(args: argparse.Namespace) -> dict[str, Any]:
    """Describe the effective Sionna setup in a UI-friendly stable schema."""

    motion = configured_motion(args)
    _, downlink_links, uplink_links, crosstalk_links, links = link_layout(
        args.layout
    )
    return {
        "layout": args.layout,
        "scene": args.scene,
        "simple_road": {
            "enabled": args.simple_road,
            "width_m": args.road_width_m if args.simple_road else None,
            "surface_material": "ITU concrete",
        },
        "solver": {
            "name": "PathSolver",
            "max_depth": args.max_depth,
            "samples_per_source": args.samples_per_src,
            "seed": args.seed,
            "synthetic_array": True,
            "propagation": {
                "los": True,
                "specular_reflection": True,
                "diffuse_reflection": False,
                "refraction": False,
                "diffraction": False,
            },
        },
        "antenna": {
            "tx": "1x1 isotropic, vertical polarization",
            "rx": "1x1 isotropic, vertical polarization",
        },
        "sample_rate_hz": args.sample_rate_hz,
        "update_rate_hz": args.update_hz,
        "gain_offset_db": args.gain_offset_db,
        "frequencies_hz": {
            "downlink": args.downlink_frequency_hz,
            "uplink": args.uplink_frequency_hz,
        },
        "nodes": {
            node_id: {
                "start_m": item.start,
                "velocity_mps": item.velocity,
                "speed_mps": math.sqrt(sum(value * value for value in item.velocity)),
                "mobility": item.mobility,
                "route_x_m": item.x_bounds,
            }
            for node_id, item in motion.items()
        },
        "link_count": len(links),
        "link_groups": {
            "downlink": len(downlink_links),
            "uplink": len(uplink_links),
            "ue_crosstalk": len(crosstalk_links),
        },
        "control_endpoint": None if args.dry_run else args.control_endpoint,
        "dry_run": args.dry_run,
    }


def parse_vector(text: str) -> tuple[float, float, float]:
    try:
        values = tuple(float(piece.strip()) for piece in text.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected x,y,z numeric vector") from exc
    if len(values) != 3 or not all(math.isfinite(value) for value in values):
        raise argparse.ArgumentTypeError("expected exactly three finite values: x,y,z")
    return values  # type: ignore[return-value]


def parse_range(text: str) -> tuple[float, float]:
    try:
        values = tuple(float(piece.strip()) for piece in text.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected min,max numeric range") from exc
    if (
        len(values) != 2
        or not all(math.isfinite(value) for value in values)
        or values[0] >= values[1]
    ):
        raise argparse.ArgumentTypeError("expected two finite values with min < max")
    return values  # type: ignore[return-value]


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-endpoint", default="tcp://127.0.0.1:5559")
    parser.add_argument(
        "--layout",
        choices=("2x2", "1x1"),
        default="2x2",
        help="emulator graph driven by Sionna RT (default: 2x2)",
    )
    parser.add_argument("--scene", default="simple_street_canyon")
    parser.add_argument("--duration", type=float, default=30.0,
                        help="wall-clock seconds; 0 runs until interrupted")
    parser.add_argument("--iterations", type=int, default=0,
                        help="optional update-count cap; 0 means no cap")
    parser.add_argument("--update-hz", type=float, default=2.0)
    parser.add_argument("--sample-rate-hz", type=float, default=23_040_000.0)
    parser.add_argument(
        "--downlink-frequency-hz",
        type=float,
        default=DEFAULT_DOWNLINK_FREQUENCY_HZ,
        help="Sionna carrier for gNB-to-UE links (default: band-3 DL)",
    )
    parser.add_argument(
        "--uplink-frequency-hz",
        type=float,
        default=DEFAULT_UPLINK_FREQUENCY_HZ,
        help="Sionna carrier for UE-to-gNB links (default: band-3 UL)",
    )
    parser.add_argument(
        "--carrier-frequency-hz",
        type=float,
        help="compatibility/TDD override: use one carrier for both directions",
    )
    parser.add_argument("--gain-offset-db", type=float, default=60.0,
                        help="fixed Sionna-field to normalized-IQ calibration")
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--samples-per-src", type=int, default=200_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--simple-road",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="split the built-in floor into a visible road and two ground strips",
    )
    parser.add_argument("--road-width-m", type=float, default=DEFAULT_ROAD_WIDTH_M)
    parser.add_argument(
        "--gnb-height-m",
        type=float,
        default=DEFAULT_GNB_HEIGHT_M,
        help="shared gNB antenna height; default clears the 51 m rooftops",
    )
    parser.add_argument("--ue0-start", type=parse_vector,
                        default=DEFAULT_MOTION["ue0"].start)
    parser.add_argument("--ue1-start", type=parse_vector,
                        default=DEFAULT_MOTION["ue1"].start)
    parser.add_argument("--ue0-velocity", type=parse_vector,
                        default=DEFAULT_MOTION["ue0"].velocity)
    parser.add_argument("--ue1-velocity", type=parse_vector,
                        default=DEFAULT_MOTION["ue1"].velocity)
    parser.add_argument("--ue0-route-x", type=parse_range,
                        default=DEFAULT_ROUTE_X_M,
                        help="car ping-pong route bounds in meters: min,max")
    parser.add_argument("--ue1-route-x", type=parse_range,
                        default=DEFAULT_ROUTE_X_M,
                        help="pedestrian ping-pong route bounds in meters: min,max")
    parser.add_argument("--status-jsonl", type=pathlib.Path,
                        help="append full per-update channel status as JSON Lines")
    parser.add_argument("--dry-run", action="store_true",
                        help="trace and print control JSON without connecting to ZMQ")
    args = parser.parse_args(argv)
    if args.duration < 0.0:
        parser.error("--duration must be >= 0")
    if args.iterations < 0:
        parser.error("--iterations must be >= 0")
    if args.update_hz <= 0.0:
        parser.error("--update-hz must be positive")
    if args.carrier_frequency_hz is not None:
        args.downlink_frequency_hz = args.carrier_frequency_hz
        args.uplink_frequency_hz = args.carrier_frequency_hz
    if (
        args.sample_rate_hz <= 0.0
        or args.downlink_frequency_hz <= 0.0
        or args.uplink_frequency_hz <= 0.0
    ):
        parser.error("sample rate and DL/UL carrier frequencies must be positive")
    if args.max_depth < 0 or args.samples_per_src <= 0:
        parser.error("max depth must be non-negative and samples-per-src positive")
    if args.road_width_m <= 0.0:
        parser.error("--road-width-m must be positive")
    if not math.isfinite(args.gnb_height_m) or args.gnb_height_m <= 0.0:
        parser.error("--gnb-height-m must be finite and positive")
    for node_id in ("ue0", "ue1"):
        start_x = getattr(args, f"{node_id}_start")[0]
        low, high = getattr(args, f"{node_id}_route_x")
        if not low <= start_x <= high:
            parser.error(f"--{node_id}-start x must be inside --{node_id}-route-x")
    return args


_PLY_SCALAR_FORMATS = {
    "char": "b",
    "int8": "b",
    "uchar": "B",
    "uint8": "B",
    "short": "h",
    "int16": "h",
    "ushort": "H",
    "uint16": "H",
    "int": "i",
    "int32": "i",
    "uint": "I",
    "uint32": "I",
    "float": "f",
    "float32": "f",
    "double": "d",
    "float64": "d",
}


def ply_bounds(path: pathlib.Path) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Read only the vertex XYZ bounds from an ASCII or binary PLY mesh."""

    with path.open("rb") as handle:
        if handle.readline().strip() != b"ply":
            raise ValueError(f"not a PLY file: {path}")
        encoding = ""
        vertex_count = 0
        vertex_properties: list[str] = []
        in_vertices = False
        while True:
            raw = handle.readline()
            if not raw:
                raise ValueError(f"truncated PLY header: {path}")
            line = raw.decode("ascii").strip()
            fields = line.split()
            if fields[:1] == ["format"]:
                encoding = fields[1]
            elif fields[:2] == ["element", "vertex"]:
                vertex_count = int(fields[2])
                in_vertices = True
            elif fields[:1] == ["element"]:
                in_vertices = False
            elif in_vertices and fields[:1] == ["property"]:
                if len(fields) != 3 or fields[1] not in _PLY_SCALAR_FORMATS:
                    raise ValueError(f"unsupported PLY vertex property: {line}")
                vertex_properties.append(fields[1])
            elif line == "end_header":
                break

        if vertex_count <= 0 or len(vertex_properties) < 3:
            raise ValueError(f"PLY has no XYZ vertices: {path}")
        vertices: list[tuple[float, float, float]] = []
        if encoding == "ascii":
            for _ in range(vertex_count):
                fields = handle.readline().split()
                vertices.append(tuple(float(value) for value in fields[:3]))  # type: ignore[arg-type]
        elif encoding in ("binary_little_endian", "binary_big_endian"):
            prefix = "<" if encoding == "binary_little_endian" else ">"
            row = struct.Struct(
                prefix + "".join(_PLY_SCALAR_FORMATS[item] for item in vertex_properties)
            )
            for _ in range(vertex_count):
                values = row.unpack(handle.read(row.size))
                vertices.append(tuple(float(value) for value in values[:3]))  # type: ignore[arg-type]
        else:
            raise ValueError(f"unsupported PLY encoding {encoding!r}: {path}")

    minimum = tuple(min(vertex[index] for vertex in vertices) for index in range(3))
    maximum = tuple(max(vertex[index] for vertex in vertices) for index in range(3))
    return minimum, maximum  # type: ignore[return-value]


def write_rectangle_ply(
    path: pathlib.Path,
    *,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    z: float,
) -> None:
    """Write one upward-facing rectangular surface as a tiny ASCII PLY."""

    body = f"""ply
format ascii 1.0
element vertex 4
property float x
property float y
property float z
element face 2
property list uchar int vertex_indices
end_header
{x_min} {y_min} {z}
{x_max} {y_min} {z}
{x_max} {y_max} {z}
{x_min} {y_max} {z}
3 0 1 2
3 0 2 3
"""
    path.write_text(body, encoding="ascii")


def prepare_scene_with_simple_road(
    scene_xml: pathlib.Path,
    output_directory: pathlib.Path,
    road_width_m: float,
) -> pathlib.Path:
    """Replace the scene floor with non-overlapping road/ground surfaces."""

    tree = ET.parse(scene_xml)
    root = tree.getroot()
    source_directory = scene_xml.parent
    floor_shape: ET.Element | None = None
    floor_mesh: pathlib.Path | None = None
    floor_material = "concrete"

    for shape in root.findall("shape"):
        filename = shape.find("./string[@name='filename']")
        if filename is None or "value" not in filename.attrib:
            continue
        absolute = (source_directory / filename.attrib["value"]).resolve()
        filename.set("value", str(absolute))
        if shape.attrib.get("id") == "mesh-floor":
            floor_shape = shape
            floor_mesh = absolute
            material_ref = shape.find("./ref[@name='bsdf']")
            if material_ref is not None:
                floor_material = material_ref.attrib.get("id", floor_material)

    if floor_shape is None or floor_mesh is None:
        raise RuntimeError("--simple-road requires a scene shape named mesh-floor")
    minimum, maximum = ply_bounds(floor_mesh)
    center_y = 0.5 * (minimum[1] + maximum[1])
    road_y_min = center_y - 0.5 * road_width_m
    road_y_max = center_y + 0.5 * road_width_m
    if road_y_min <= minimum[1] or road_y_max >= maximum[1]:
        raise RuntimeError("road width must be smaller than the scene floor")

    root.remove(floor_shape)
    road_material_id = "road-surface"
    road_material = ET.Element(
        "bsdf", {"type": "itu-radio-material", "id": road_material_id}
    )
    ET.SubElement(road_material, "string", {"name": "type", "value": "concrete"})
    ET.SubElement(road_material, "float", {"name": "thickness", "value": "0.1"})
    first_shape_index = next(
        (index for index, child in enumerate(root) if child.tag == "shape"), len(root)
    )
    root.insert(first_shape_index, road_material)

    surfaces = (
        ("ground-south", minimum[1], road_y_min, floor_material),
        ("road", road_y_min, road_y_max, road_material_id),
        ("ground-north", road_y_max, maximum[1], floor_material),
    )
    for name, y_min, y_max, material in surfaces:
        mesh_path = output_directory / f"{name}.ply"
        write_rectangle_ply(
            mesh_path,
            x_min=minimum[0],
            x_max=maximum[0],
            y_min=y_min,
            y_max=y_max,
            z=maximum[2],
        )
        shape = ET.SubElement(root, "shape", {"type": "ply", "id": f"mesh-{name}"})
        ET.SubElement(
            shape, "string", {"name": "filename", "value": str(mesh_path.resolve())}
        )
        ET.SubElement(shape, "boolean", {"name": "face_normals", "value": "true"})
        ET.SubElement(shape, "ref", {"id": material, "name": "bsdf"})

    output_xml = output_directory / "simple_street_canyon_with_road.xml"
    tree.write(output_xml, encoding="utf-8", xml_declaration=True)
    return output_xml


def scene_geometry(scene_xml: pathlib.Path) -> dict[str, Any]:
    """Extract compact top-down footprints for the UI, never the GPU control path."""

    root = ET.parse(scene_xml).getroot()
    source_directory = scene_xml.parent
    materials: dict[str, str] = {}
    for material in root.findall("bsdf"):
        material_id = material.attrib.get("id")
        itu_type = material.find("./string[@name='type']")
        if material_id:
            materials[material_id] = (
                itu_type.attrib.get("value", material_id)
                if itu_type is not None
                else material_id
            )

    objects: list[dict[str, Any]] = []
    for shape in root.findall("shape"):
        filename = shape.find("./string[@name='filename']")
        if filename is None or not filename.attrib.get("value"):
            continue
        mesh_path = pathlib.Path(filename.attrib["value"])
        if not mesh_path.is_absolute():
            mesh_path = source_directory / mesh_path
        minimum, maximum = ply_bounds(mesh_path)
        object_id = shape.attrib.get("id", mesh_path.stem).removeprefix("mesh-")
        if object_id.startswith("building"):
            kind = "building"
        elif object_id == "road":
            kind = "road"
        else:
            kind = "ground"
        material_ref = shape.find("./ref[@name='bsdf']")
        material_id = material_ref.attrib.get("id", "") if material_ref is not None else ""
        item: dict[str, Any] = {
            "id": object_id,
            "kind": kind,
            "material": materials.get(material_id, material_id),
            "footprint_xy_m": [
                [minimum[0], minimum[1]],
                [maximum[0], minimum[1]],
                [maximum[0], maximum[1]],
                [minimum[0], maximum[1]],
            ],
            "z_min_m": minimum[2],
            "z_max_m": maximum[2],
        }
        if kind == "road":
            center_y = 0.5 * (minimum[1] + maximum[1])
            item["centerline_xy_m"] = [
                [minimum[0], center_y],
                [maximum[0], center_y],
            ]
        objects.append(item)
    return {
        "coordinate_system": "Sionna XYZ, meters",
        "projection": "top_down_xy",
        "objects": objects,
    }


def import_sionna() -> tuple[Any, Any]:
    try:
        import numpy as np  # type: ignore
        import sionna.rt as rt  # type: ignore
    except ImportError as exc:  # pragma: no cover - live dependency path
        raise RuntimeError(
            "Sionna RT is not installed. Run: "
            "python3 -m pip install -r scripts/sionna_rt/requirements.txt"
        ) from exc
    return rt, np


def numpy_value(value: Any, np: Any) -> Any:
    if hasattr(value, "numpy"):
        value = value.numpy()
    return np.asarray(value)


def siso_slice(array: Any, rx_index: int, tx_index: int, *, has_time: bool) -> Any:
    """Index either synthetic-array or center-device Sionna tensor layouts."""

    if has_time:
        if array.ndim == 6:
            return array[rx_index, 0, tx_index, 0, :, 0]
        if array.ndim == 4:
            return array[rx_index, tx_index, :, 0]
    else:
        if array.ndim == 5:
            return array[rx_index, 0, tx_index, 0, :]
        if array.ndim == 3:
            return array[rx_index, tx_index, :]
    raise RuntimeError(f"unexpected Sionna tensor rank {array.ndim}")


class SionnaScenario:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.rt, self.np = import_sionna()
        try:
            scene_path = getattr(self.rt.scene, args.scene)
        except AttributeError as exc:
            raise RuntimeError(f"unknown built-in Sionna scene: {args.scene}") from exc

        self._scene_workspace: tempfile.TemporaryDirectory[str] | None = None
        scene_xml = pathlib.Path(scene_path)
        if args.simple_road:
            self._scene_workspace = tempfile.TemporaryDirectory(
                prefix="ocudu-sionna-road-"
            )
            scene_xml = prepare_scene_with_simple_road(
                scene_xml,
                pathlib.Path(self._scene_workspace.name),
                args.road_width_m,
            )
        self.scene_geometry = scene_geometry(scene_xml)
        self.scene = self.rt.load_scene(str(scene_xml), merge_shapes=True)
        self.scene.frequency = args.downlink_frequency_hz
        self.scene.bandwidth = args.sample_rate_hz
        self.scene.tx_array = self.rt.PlanarArray(
            num_rows=1, num_cols=1, pattern="iso", polarization="V"
        )
        self.scene.rx_array = self.rt.PlanarArray(
            num_rows=1, num_cols=1, pattern="iso", polarization="V"
        )

        self.motion = configured_motion(args)
        (
            self.node_ids,
            self.downlink_links,
            self.uplink_links,
            self.crosstalk_links,
            self.links,
        ) = link_layout(args.layout)
        self.current_velocities = {
            node_id: motion.velocity_at(0.0)
            for node_id, motion in self.motion.items()
        }

        # Each emulator node appears once as a Sionna transmitter and once as
        # a receiver. FDD runs one solve per direction so frequency-dependent
        # materials and path coefficients match the configured UL/DL carrier.
        for node_id in self.node_ids:
            position = self.motion[node_id].start
            tx = self.rt.Transmitter(name=f"{node_id}_tx", position=position)
            rx = self.rt.Receiver(name=f"{node_id}_rx", position=position)
            tx.velocity = self.motion[node_id].velocity_at(0.0)
            rx.velocity = self.motion[node_id].velocity_at(0.0)
            self.scene.add(tx)
            self.scene.add(rx)

        self.tx_indices = {
            name.removesuffix("_tx"): index
            for index, name in enumerate(self.scene.transmitters.keys())
        }
        self.rx_indices = {
            name.removesuffix("_rx"): index
            for index, name in enumerate(self.scene.receivers.keys())
        }
        self.solver = self.rt.PathSolver()

    def update_positions(self, elapsed_seconds: float) -> dict[str, tuple[float, float, float]]:
        positions: dict[str, tuple[float, float, float]] = {}
        for node_id, motion in self.motion.items():
            position = motion.position_at(elapsed_seconds)
            velocity = motion.velocity_at(elapsed_seconds)
            positions[node_id] = position
            self.current_velocities[node_id] = velocity
            self.scene.get(f"{node_id}_tx").position = position
            self.scene.get(f"{node_id}_rx").position = position
            self.scene.get(f"{node_id}_tx").velocity = velocity
            self.scene.get(f"{node_id}_rx").velocity = velocity
        return positions

    def trace(self, carrier_frequency_hz: float) -> Any:
        self.scene.frequency = carrier_frequency_hz
        return self.solver(
            scene=self.scene,
            max_depth=self.args.max_depth,
            samples_per_src=self.args.samples_per_src,
            synthetic_array=True,
            los=True,
            specular_reflection=True,
            diffuse_reflection=False,
            refraction=False,
            diffraction=False,
            seed=self.args.seed,
        )

    def profiles(
        self,
        paths: Any,
        links: Sequence[tuple[str, str]],
        *,
        direction: str,
        carrier_frequency_hz: float,
    ) -> tuple[dict[str, list[Tap]], list[dict[str, Any]]]:
        coefficients, delays = paths.cir(
            sampling_frequency=self.args.update_hz,
            num_time_steps=1,
            normalize_delays=False,
            out_type="numpy",
        )
        coefficients = numpy_value(coefficients, self.np)
        delays = numpy_value(delays, self.np)
        valid = numpy_value(paths.valid, self.np)

        profiles: dict[str, list[Tap]] = {}
        statuses: list[dict[str, Any]] = []
        for source, destination in links:
            tx_index = self.tx_indices[source]
            rx_index = self.rx_indices[destination]
            coeff_slice = siso_slice(coefficients, rx_index, tx_index, has_time=True)
            delay_slice = siso_slice(delays, rx_index, tx_index, has_time=False)
            valid_slice = siso_slice(valid, rx_index, tx_index, has_time=False)
            rays = [
                Ray(float(delay), complex(coefficient))
                for coefficient, delay, is_valid in zip(
                    coeff_slice, delay_slice, valid_slice
                )
                if bool(is_valid)
            ]
            link_id = control_link_id(source, destination, MODEL_ID)
            taps = rays_to_taps(
                rays,
                sample_rate_hz=self.args.sample_rate_hz,
                gain_offset_db=self.args.gain_offset_db,
            )
            profiles[link_id] = taps
            status = channel_status(
                link_id,
                rays,
                taps,
                sample_rate_hz=self.args.sample_rate_hz,
            )
            status["source"] = source
            status["destination"] = destination
            status["model"] = MODEL_ID
            status["direction"] = direction
            status["carrier_frequency_hz"] = carrier_frequency_hz
            statuses.append(status)
        return profiles, statuses

    def trace_all_profiles(
        self,
    ) -> tuple[dict[str, list[Tap]], list[dict[str, Any]], dict[str, float]]:
        """Trace desired/inter-cell and UE-to-UE crosstalk profiles."""

        timings_ms: dict[str, float] = {}
        profiles: dict[str, list[Tap]] = {}
        statuses: list[dict[str, Any]] = []

        # A common carrier is useful for TDD and for compatibility with the
        # former --carrier-frequency-hz behavior. In that case one solve can
        # cover all ten directed links.
        if self.args.downlink_frequency_hz == self.args.uplink_frequency_hz:
            started = time.monotonic()
            paths = self.trace(self.args.downlink_frequency_hz)
            one_profiles, one_statuses = self.profiles(
                paths,
                self.links,
                direction="bidirectional",
                carrier_frequency_hz=self.args.downlink_frequency_hz,
            )
            timings_ms["bidirectional"] = (time.monotonic() - started) * 1000.0
            return one_profiles, one_statuses, timings_ms

        for direction, frequency_hz, links in (
            ("downlink", self.args.downlink_frequency_hz, self.downlink_links),
            ("uplink", self.args.uplink_frequency_hz, self.uplink_links),
            ("crosstalk", self.args.uplink_frequency_hz, self.crosstalk_links),
        ):
            if not links:
                continue
            started = time.monotonic()
            paths = self.trace(frequency_hz)
            direction_profiles, direction_statuses = self.profiles(
                paths,
                links,
                direction=direction,
                carrier_frequency_hz=frequency_hz,
            )
            timings_ms[direction] = (time.monotonic() - started) * 1000.0
            profiles.update(direction_profiles)
            statuses.extend(direction_statuses)
        return profiles, statuses, timings_ms


def append_status(path: pathlib.Path | None, record: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    *_, configured_links = link_layout(args.layout)
    process_id = os.getpid()
    session_id = f"sionna-rt-{process_id}-{time.time_ns()}"
    process_started_unix_ms = time.time_ns() // 1_000_000
    stop = False

    def publish_runtime(
        phase: str,
        *,
        iteration: int = 0,
        detail: str | None = None,
        next_update_in_ms: float | None = None,
    ) -> None:
        """Publish lifecycle state without sending anything to GPU Channel."""

        append_status(
            args.status_jsonl,
            {
                "event": "sionna_rt_runtime",
                "session_id": session_id,
                "process_id": process_id,
                "process_started_unix_ms": process_started_unix_ms,
                "observed_unix_ms": time.time_ns() // 1_000_000,
                "phase": phase,
                "detail": detail,
                "iteration": iteration,
                "execution_mode": "single_process_repeating_update_loop",
                "scenario_initialized_once": True,
                "channel_trace_per_iteration": True,
                "control_send_per_iteration": not args.dry_run,
                "link_count": len(configured_links),
                "target_update_hz": args.update_hz,
                "configured_duration_seconds": args.duration,
                "configured_iteration_limit": args.iterations,
                "next_update_in_ms": next_update_in_ms,
            },
        )

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    publish_runtime(
        "initializing_scene",
        detail="Sionna scene and PathSolver are being initialized once",
    )
    scenario: SionnaScenario | None = None
    client: ZmqControlClient | None = None
    start = 0.0
    next_update = 0.0
    iteration = 0
    try:
        scenario = SionnaScenario(args)
        environment = scenario_environment(args)
        client = None if args.dry_run else ZmqControlClient(args.control_endpoint)
        start = time.monotonic()
        next_update = start
        publish_runtime(
            "ready",
            detail="scene initialized; entering the repeating update loop",
        )
        while not stop:
            now = time.monotonic()
            if args.duration > 0.0 and now - start >= args.duration:
                break
            if args.iterations > 0 and iteration >= args.iterations:
                break
            if now < next_update:
                publish_runtime(
                    "waiting_for_next_update",
                    iteration=iteration,
                    detail="same process is waiting; the scene is not reinitialized",
                    next_update_in_ms=(next_update - now) * 1000.0,
                )
                time.sleep(next_update - now)
                if stop:
                    break
            update_started = time.monotonic()
            update_started_unix_ms = time.time_ns() // 1_000_000
            elapsed = time.monotonic() - start
            positions = scenario.update_positions(elapsed)
            generation_started = time.monotonic()
            publish_runtime(
                "tracing_channels",
                iteration=iteration,
                detail="recomputing all directed Sionna RT paths for this update",
            )
            profiles, statuses, direction_trace_ms = scenario.trace_all_profiles()
            channel_generation_ms = (time.monotonic() - generation_started) * 1000.0
            batch_id = f"sionna-{iteration}"

            if client is not None:
                publish_runtime(
                    "sending_control",
                    iteration=iteration,
                    detail=f"sending the {len(configured_links)}-link profile batch and waiting for ACK",
                )
                control_started = time.monotonic()
                reply = client.send_profiles(profiles, batch_id=batch_id)
                control_transaction_ms: float | None = (
                    time.monotonic() - control_started
                ) * 1000.0
                generate_to_control_ack_ms: float | None = (
                    time.monotonic() - generation_started
                ) * 1000.0
                control_ack_unix_ms: int | None = time.time_ns() // 1_000_000
            else:
                control_transaction_ms = None
                generate_to_control_ack_ms = None
                control_ack_unix_ms = None
                reply = {
                    "ok": True,
                    "dry_run": True,
                    "batch_id": batch_id,
                    "messages": [
                        make_profile_swap(link_id, taps, batch_id=batch_id)
                        for link_id, taps in profiles.items()
                    ],
                }

            total_update_ms = (time.monotonic() - update_started) * 1000.0

            record = {
                "event": "sionna_rt_update",
                "session_id": session_id,
                "process_id": process_id,
                "iteration": iteration,
                "batch_id": batch_id,
                "elapsed_seconds": elapsed,
                "update_started_unix_ms": update_started_unix_ms,
                "control_ack_unix_ms": control_ack_unix_ms,
                # Keep trace_ms/direction_trace_ms for existing readers.
                "trace_ms": channel_generation_ms,
                "direction_trace_ms": direction_trace_ms,
                "timing_ms": {
                    "channel_generation": channel_generation_ms,
                    "control_transaction": control_transaction_ms,
                    "generate_to_control_ack": generate_to_control_ack_ms,
                    "total_update": total_update_ms,
                    "directions": direction_trace_ms,
                },
                "environment": environment,
                # UI-only scene footprints. They are written to status JSONL
                # and never included in profile_swap control messages.
                "scene_geometry": scenario.scene_geometry,
                "frequencies_hz": {
                    "downlink": args.downlink_frequency_hz,
                    "uplink": args.uplink_frequency_hz,
                },
                "positions": positions,
                "velocities_mps": scenario.current_velocities,
                "channels": statuses,
                "control_reply": reply,
            }
            print(json.dumps(record, separators=(",", ":")), flush=True)
            append_status(args.status_jsonl, record)
            iteration += 1
            next_update += 1.0 / args.update_hz
            # A slow trace must not produce a burst of stale updates.
            next_update = max(next_update, time.monotonic())
        publish_runtime(
            "stopped",
            iteration=iteration,
            detail="configured limit reached or stop signal received",
        )
    except Exception as exc:
        publish_runtime(
            "failed",
            iteration=iteration,
            detail=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        if client is not None:
            client.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "sionna_rt_fatal",
                    "exception_type": type(exc).__name__,
                    "error": str(exc),
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        traceback.print_exc(file=sys.stderr)
        raise SystemExit(1)
