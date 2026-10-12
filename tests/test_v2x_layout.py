"""One geometry for SUMO, Sionna and the controller: reference points, the
generated scene/scenarios, and (with SUMO installed) the generated network."""

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "v2x_intersection"
SCENE = ROOT / "examples" / "configs" / "sionna" / "scenes" / "v2x_intersection"
SCENARIOS = ROOT / "examples" / "configs" / "sionna" / "scenarios" / "v2x_intersection"
sys.path.insert(0, str(EXAMPLE))

import layout  # noqa: E402


def test_conflict_point_is_on_both_lanes():
    assert layout.CONFLICT_POINT_M == (layout.A_LANE_X_M, layout.B_LANE_Y_M)
    assert layout.A_START_M[0] == layout.A_LANE_X_M
    assert layout.B_START_M[1] == layout.B_LANE_Y_M


def test_blind_corner_hides_b_until_close():
    a_far, b_far = (layout.A_LANE_X_M, -60.0), (60.0, layout.B_LANE_Y_M)
    assert not layout.line_of_sight(a_far, b_far)           # SE block in between
    a_near, b_near = (layout.A_LANE_X_M, -9.0), (9.0, layout.B_LANE_Y_M)
    assert layout.line_of_sight(a_near, b_near)


def test_radio_sites_give_the_intended_line_of_sight():
    los = layout.RADIO_SITES["los"].gnb_m[:2]
    blocked = layout.RADIO_SITES["blocked"].gnb_m[:2]
    ys = [-100.0 + k for k in range(0, 101)]
    assert all(layout.line_of_sight(los, (layout.A_LANE_X_M, y)) for y in ys)
    visible = [y for y in ys if layout.line_of_sight(blocked, (layout.A_LANE_X_M, y))]
    # The SW block corner (-8, -70) cuts the view at y = -140 + 110/32 * 41.75 = -48.5.
    assert max(visible) == pytest.approx(-49.0, abs=1.0)
    assert min(visible) == -100.0
    for site in layout.RADIO_SITES.values():
        assert layout.line_of_sight(site.gnb_m[:2], site.parked_ue_m[:2])


def test_generated_scene_and_scenarios_match_layout(tmp_path):
    manifest = json.loads((SCENE / "manifest.json").read_text())
    blocks = {b["id"]: b for b in manifest["blocks"]}
    for block in layout.BLOCKS:
        got = blocks[f"building_{block.name}"]
        assert got["x_m"] == [block.x_min, block.x_max]
        assert got["y_m"] == [block.y_min, block.y_max]
    for name, site in layout.RADIO_SITES.items():
        scenario = json.loads((SCENARIOS / f"v2x-intersection-{name}.json").read_text())
        assert scenario["scene"] == "v2x_intersection"
        assert scenario["nodes"]["gnb0"]["start_m"] == list(site.gnb_m)
        assert scenario["nodes"]["ue0"]["start_m"] == [*layout.A_START_M, layout.ANTENNA_HEIGHT_M]
        assert scenario["nodes"]["ue1"]["start_m"] == list(site.parked_ue_m)
    # The committed files are what the builder produces now.
    before = {p: p.read_bytes() for p in list(SCENE.glob("*.json")) + list(SCENE.glob("*.xml"))
              + list(SCENARIOS.glob("*.json"))}
    subprocess.run([sys.executable, str(SCENE / "build_v2x_intersection.py")], check=True, capture_output=True)
    for path, data in before.items():
        assert path.read_bytes() == data, f"{path} is stale; rerun build_v2x_intersection.py"


def test_sumo_network_uses_the_scene_frame(tmp_path):
    pytest.importorskip("libsumo", reason="SUMO is external; see setup-external.sh")
    import sumo_world

    net = sumo_world.sumolib.net.readNet(str(sumo_world.build_network(tmp_path)))
    assert net.getLocationOffset() == (0.0, 0.0) or list(net.getLocationOffset()) == [0.0, 0.0]
    assert net.getNode("C").getCoord() == (0.0, 0.0)
    a_lane = net.getLane("S2C_0").getShape()
    b_lane = net.getLane("E2C_0").getShape()
    assert all(x == pytest.approx(layout.A_LANE_X_M) for x, _ in a_lane)
    assert all(y == pytest.approx(layout.B_LANE_Y_M) for _, y in b_lane)
    assert net.getNode("C").getType() == "unregulated"
