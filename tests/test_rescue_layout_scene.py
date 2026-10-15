"""One building: layout.py <-> the generated Sionna scene, manifest and scenarios."""

import json
import pathlib
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "multi_robot_rescue"))

import layout  # noqa: E402

SCENE = ROOT / "examples" / "configs" / "sionna" / "scenes" / "multi_robot_rescue"
SCENARIOS = ROOT / "examples" / "configs" / "sionna" / "scenarios" / "multi_robot_rescue"


def test_manifest_matches_layout():
    manifest = json.loads((SCENE / "manifest.json").read_text())
    boxes = {b["id"]: b for b in manifest["boxes"]}
    for b in (layout.ROOF,) + layout.WALLS + layout.RUBBLE:
        m = boxes[b.name]
        assert m["x_m"] == [b.x0, b.x1] and m["y_m"] == [b.y0, b.y1] and m["z_m"] == [b.z0, b.z1]
        assert m["material"] == b.material
    assert len(boxes) == 1 + len(layout.WALLS) + len(layout.RUBBLE)
    assert manifest["shadow_room"] == layout.SHADOW_ROOM


def test_scene_xml_references_every_mesh_and_material():
    root = ET.parse(SCENE / "scene.xml").getroot()
    materials = {b.get("id"): b.find("string").get("value") for b in root.findall("bsdf")}
    for material, (itu, _thickness) in layout.MATERIALS.items():
        if material in materials:
            assert materials[material] == itu
    shapes = root.findall("shape")
    assert len(shapes) == 2 + len(layout.WALLS) + len(layout.RUBBLE)      # + ground, roof
    for shape in shapes:
        assert (SCENE / shape.find("string").get("value")).is_file()
        assert shape.find("ref").get("id") in materials


@pytest.mark.parametrize("robots,ue_count", [(1, 2), (2, 2), (3, 4)])
@pytest.mark.parametrize("site", sorted(layout.RADIO_SITES))
def test_scenarios_follow_layout(site, robots, ue_count):
    scenario = json.loads((SCENARIOS / f"multi-robot-rescue-{site}-{robots}robot.json").read_text())
    assert scenario["scene"] == "multi_robot_rescue"
    nodes = scenario["nodes"]
    assert nodes["gnb0"]["start_m"] == list(layout.RADIO_SITES[site].gnb_m)
    assert sorted(n for n in nodes if n.startswith("ue")) == [f"ue{i}" for i in range(ue_count)]
    for index in range(robots):
        rid = layout.ROBOT_IDS[index]
        assert layout.UE_NODE[rid] == f"ue{index}"
        x, y, _ = layout.SPAWN_POSES[rid]
        assert nodes[f"ue{index}"]["start_m"] == [x, y, layout.ROBOT_ANTENNA_HEIGHT_M]
    for index in range(robots, ue_count):
        assert nodes[f"ue{index}"]["mobility"] == "fixed"
        assert not layout.inside_building(*nodes[f"ue{index}"]["start_m"][:2])
    assert len(scenario["links"]) == 2 * ue_count
    assert scenario["solver"]["propagation"]["refraction"] is True


def test_spawns_and_survivor_presets_are_free():
    for x, y, _ in layout.SPAWN_POSES.values():
        assert not layout.blocked(x, y, layout.ROBOT_RADIUS_M)
    for preset in layout.SURVIVOR_PRESETS:
        for x, y in layout.survivor_positions(preset, 0):
            assert layout.inside_building(x, y) and not layout.blocked(x, y, layout.SURVIVOR_RADIUS_M)
    assert layout.room_of(*layout.SURVIVOR_PRESETS["demo"][0]) == layout.SHADOW_ROOM
    for seed in range(20):
        a, b = layout.survivor_positions("random", seed)
        assert layout.room_of(*a) != layout.room_of(*b)
