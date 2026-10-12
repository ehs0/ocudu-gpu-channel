"""Whole episodes in SUMO (skipped without the external SUMO install).

Lockstep episodes check the controller-in-SUMO behaviour without any
communication; the loopback test runs the real processes (roadside server,
UE client, drive.py) on 127.0.0.1 in wall-clock time — no radio.
"""

import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "v2x_intersection"
sys.path.insert(0, str(EXAMPLE))
pytest.importorskip("libsumo", reason="SUMO is external; see setup-external.sh")


def drive(tmp_path, *words):
    out = tmp_path / "ep"
    subprocess.run([sys.executable, str(EXAMPLE / "drive.py"), "--out", str(out), "--lockstep", *words],
                   check=True, capture_output=True, text=True)
    return json.loads((out / "result.json").read_text())


def test_blind_crossing_without_information_collides(tmp_path):
    result = drive(tmp_path, "--no-v2x", "--fallback", "none")
    assert result["collision"] is True
    assert result["first_brake_sim_time_s"] is not None      # the sensor saw B, too late


def test_cautious_fallback_avoids_it_at_a_time_cost(tmp_path):
    result = drive(tmp_path, "--no-v2x", "--fallback", "cautious")
    assert result["collision"] is False
    assert result["order"] == "B_first"
    # At cruise speed A's tail would clear the conflict point after (101.75 + 4.5) / 13.89 = 7.65 s.
    assert result["a_crossed_sim_time_s"] > 8.5


@pytest.mark.skipif(os.environ.get("V2X_SKIP_WALLCLOCK") == "1", reason="wall-clock test disabled")
def test_loopback_v2x_prevents_the_collision(tmp_path):
    subprocess.run([sys.executable, str(EXAMPLE / "run_local.py"), "--out", str(tmp_path / "run"),
                    "--modes", "v2x", "--drive-args", "--fallback none", "--port", "7199"],
                   check=True, capture_output=True, text=True, timeout=120)
    (episode,) = (tmp_path / "run" / "episodes").iterdir()
    result = json.loads((episode / "result.json").read_text())
    assert result["v2x"]["messages_received"] > 50
    assert result["collision"] is False
    assert result["first_risk_sim_time_s"] < 1.0               # warned long before the sensor could see B
    assert result["timing"]["late_steps"] == 0
