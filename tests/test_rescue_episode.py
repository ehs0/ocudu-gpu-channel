"""One short loopback episode with an injected blackout of robot_2 (all real processes, no radio).

The shadow-region policy end to end: while its link is OFFLINE robot_2 keeps
exploring on its own, the mission server receives nothing from it, and what it
captured meanwhile is delivered after the link recovers, once.
"""

import csv
import json
import math
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "multi_robot_rescue"

pytestmark = pytest.mark.skipif(os.environ.get("RESCUE_SKIP_WALLCLOCK") == "1", reason="wall-clock test disabled")


@pytest.fixture(scope="module")
def episode(tmp_path_factory):
    out = tmp_path_factory.mktemp("rescue")
    subprocess.run([sys.executable, str(EXAMPLE / "run_local.py"), "--out", str(out), "--network", "blackout",
                    "--blackout", "robot_2:8-28", "--time-limit", "60", "--coverage-target", "1.1",
                    "--port-base", "7800", "--drain-s", "8"],
                   check=False, capture_output=True, text=True, timeout=180)
    (path,) = (out / "episodes").iterdir()
    return path


def rows(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def offline_interval(ep):
    states = rows(ep / "links" / "robot_2" / "link_state.csv")
    off = next(int(r["t_ms"]) for r in states if r["to"] == "OFFLINE")
    on = next(int(r["t_ms"]) for r in states if r["from"] == "OFFLINE" and int(r["t_ms"]) > off)
    return off, on


def test_world_kept_real_time(episode):
    result = json.loads((episode / "sim" / "result.json").read_text())
    assert result["end_reason"] == "time_limit"
    assert 0.98 <= result["timing"]["rtf"] <= 1.02
    assert result["timing"]["lockstep"] is False


def test_robot_keeps_exploring_while_offline(episode):
    off, on = offline_interval(episode)
    assert on - off >= 8_000
    poses = [r for r in rows(episode / "sim" / "poses.csv") if r["robot_id"] == "robot_2" and off <= int(r["t_ms"]) <= on]
    moved = sum(math.hypot(float(b["x"]) - float(a["x"]), float(b["y"]) - float(a["y"])) for a, b in zip(poses, poses[1:]))
    assert moved > 0.25 * (on - off) / 1000          # well above standing still (cruise 0.6 m/s)
    log = [json.loads(line) for line in (episode / "robots" / "robot_2.jsonl").read_text().splitlines()]
    # Mode spans clipped to the outage: no fail-safe stop, and (almost) never idle.
    changes = [(m["t_ms"], m["mode"]) for m in log if m["event"] == "mode"]
    current = max((c for c in changes if c[0] <= off), default=(off, "BOOT"))[1]
    spans, since = {}, off
    for t, mode in [c for c in changes if off < c[0] <= on] + [(on, None)]:
        spans[current] = spans.get(current, 0) + (t - since)
        current, since = mode, t
    assert "SAFETY_STOP" not in spans
    assert spans.get("IDLE", 0) < 0.1 * (on - off)
    assert any(m["event"] == "goal" and m["source"] == "local" and off <= m["t_ms"] <= on for m in log)


def test_server_learns_nothing_during_the_outage_and_everything_after(episode):
    off, on = offline_interval(episode)
    proxy = rows(episode / "proxy" / "robot_2.csv")
    dropped = [int(r["t_ms"]) for r in proxy if r["fate"] == "blackout"]
    first_drop, last_drop = min(dropped), max(dropped)
    received = [json.loads(line) for line in (episode / "server" / "server.jsonl").read_text().splitlines()]
    from_robot_2 = [r for r in received if r["robot_id"] == "robot_2"]
    assert not [r for r in from_robot_2 if first_drop < r["received_ms"] < last_drop]
    captured_offline = [r for r in rows(episode / "links" / "robot_2" / "delivered.csv")
                        if off <= int(r["captured_at_ms"]) < on]
    assert captured_offline, "robot_2 generated nothing while offline"
    assert all(int(r["acked_ms"]) >= last_drop for r in captured_offline)
    ids = [r["event_id"] for r in received if not r.get("duplicate") and "other_mission" not in r]
    assert len(ids) == len(set(ids))


def test_other_robots_stay_online(episode):
    for rid in ("robot_1", "robot_3"):
        states = [r["to"] for r in rows(episode / "links" / rid / "link_state.csv")]
        assert "OFFLINE" not in states
