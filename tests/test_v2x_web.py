"""V2X dashboard: the committed page is the build output, the road API matches
the layout, and the episode follower reads a running episode."""

import csv
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB = ROOT / "examples" / "v2x_intersection" / "web"
sys.path.insert(0, str(ROOT / "examples" / "v2x_intersection"))
sys.path.insert(0, str(WEB))

import layout  # noqa: E402


def test_committed_index_is_the_build_output():
    before = (WEB / "index.html").read_bytes()
    subprocess.run([sys.executable, str(WEB / "build_index.py")], check=True, capture_output=True)
    assert (WEB / "index.html").read_bytes() == before, "index.html is stale; rerun web/build_index.py"
    page = before.decode()
    for panel in ("Timing", "Node and antenna status", "System resources · Sionna runtime loop",
                  "Estimated nominal-slot deadline utilization by receiving node",
                  "Impulse and frequency response by edge link · Tap details", "Vehicles · SUMO"):
        assert panel in page
    for dropped in ("workstream brief", "Applications &amp; outlook", "RAN KPIs by UE"):
        assert dropped not in page


def test_road_api_matches_layout():
    pytest.importorskip("libsumo", reason="SUMO is external; see setup-external.sh")
    import server

    road = server.road_geometry()
    lanes = {lane["id"]: lane for lane in road["lanes"]}
    assert all(x == pytest.approx(layout.A_LANE_X_M) for x, _ in lanes["S2C_0"]["shape"])
    assert all(y == pytest.approx(layout.B_LANE_Y_M) for _, y in lanes["E2C_0"]["shape"])
    assert road["conflict_point_m"] == list(layout.CONFLICT_POINT_M)
    assert len(road["blocks"]) == len(layout.BLOCKS)
    assert road["stop_lines"][0]["from"][1] == pytest.approx(layout.CONFLICT_POINT_M[1] - layout.STOP_LINE_GAP_M)


def test_episode_follower_reads_the_running_episode(tmp_path):
    pytest.importorskip("zmq")
    import server

    episode = tmp_path / "v2x" / "episodes" / "e000-v2x"
    episode.mkdir(parents=True)
    import drive

    with open(episode / "vehicles.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=drive.VEHICLE_FIELDS)
        writer.writeheader()
        for t in (0.05, 0.1):
            writer.writerow({"simulation_time": t, "vehicle_id": "A", "speed_mps": 13.0, "target_speed_mps": 9.5,
                             "accel_applied_mps2": -2.0, "controller_state": "WARNING", "info": "v2x"})
            writer.writerow({"simulation_time": t, "vehicle_id": "B", "speed_mps": 13.89})
    with open(episode / "v2x_rx.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=drive.RX_FIELDS)
        writer.writeheader()
        writer.writerow({"sequence": 3, "one_way_ms": 7.5, "simulation_time_at_receive": 0.1})
    snap = server.Episodes(None, tmp_path).snapshot()
    assert snap["episode"] == "e000-v2x" and snap["running"] is True
    assert snap["now"]["controller_state"] == "WARNING"
    assert snap["rx"] == [{"t": 0.1, "one_way_ms": 7.5, "seq": 3}]
    (episode / "result.json").write_text(json.dumps({"label": "v2x", "ended": "both_crossed", "collision": False}))
    snap = server.Episodes(None, tmp_path).snapshot()
    assert snap["running"] is False and snap["results"][0]["mode"] == "v2x"
