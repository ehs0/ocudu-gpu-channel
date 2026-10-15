"""UE link + mission server through an injected blackout (real processes on loopback, no radio).

A stand-in robot (this test, labelled) sends events over the robot bus to
ue_link.py; link_proxy.py drops everything in fixed windows. Checks: a
survivor report captured during the blackout is unknown to the server until
the link recovers, then arrives exactly once with its original capture time;
the link goes OFFLINE -> SYNCING -> ONLINE; flapping produces no duplicates.
"""

import csv
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "multi_robot_rescue"
sys.path.insert(0, str(EXAMPLE))

import messages  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("RESCUE_SKIP_WALLCLOCK") == "1", reason="wall-clock test disabled")


class Rig:
    def __init__(self, tmp_path, port, blackout):
        self.tmp = tmp_path
        self.run = pathlib.Path(tempfile.mkdtemp(prefix="rq-"))
        self.server_out = tmp_path / "server"
        self.links = tmp_path / "links"
        py = sys.executable
        server, proxy = f"127.0.0.1:{port}", f"127.0.0.1:{port + 1}"
        self.children = [
            subprocess.Popen([py, str(EXAMPLE / "mission_server.py"), "--bind", server, "--mission-id", "m1",
                              "--out", str(self.server_out), "--state-json", str(self.run / "state.json"),
                              "--no-planner"], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT),
            subprocess.Popen([py, str(EXAMPLE / "link_proxy.py"), "--robot-id", "robot_2", "--listen", proxy,
                              "--server", server, "--blackout", blackout, "--log", str(tmp_path / "proxy.csv")],
                             stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT),
            subprocess.Popen([py, str(EXAMPLE / "ue_link.py"), "--robot-id", "robot_2",
                              "--bind", f"127.0.0.1:{port + 2}", "--server", proxy,
                              "--bus-socket", str(self.run / "link.sock"), "--queue-db", str(self.run / "q.db"),
                              "--log-dir", str(self.links), "--ready-file", str(self.run / "ready")],
                             stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT),
        ]
        deadline = time.monotonic() + 15
        while not (self.run / "ready").exists():
            assert time.monotonic() < deadline, "ue_link did not start"
            time.sleep(0.05)
        self.t0 = time.monotonic()
        self.bus = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.bus.bind(str(self.run / "robot.sock"))
        self.seq = 0

    def send(self, type_, payload):
        self.seq += 1
        event = messages.make_event(mission_id="m1", robot_id="robot_2", boot="T1", seq=self.seq, type_=type_,
                                    captured_at_ms=messages.now_ms(), pose=(5.0, 12.0, 0.0), payload=payload)
        self.bus.sendto(messages.dumps({"kind": "event", "event": event}), str(self.run / "link.sock"))
        return event

    def received(self):
        path = self.server_out / "server.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def elapsed(self):
        return time.monotonic() - self.t0

    def close(self):
        for child in self.children:
            child.terminate()
        for child in self.children:
            child.wait(timeout=10)
        self.bus.close()


def survivor(rig, n):
    return rig.send("survivor_detected", {"candidate_id": f"robot_2-c{n}", "position_m": [30.0 + n, 20.0]})


def test_report_during_blackout_arrives_once_after_recovery(tmp_path):
    rig = Rig(tmp_path, 7710, "3-10")
    try:
        sent = None
        while rig.elapsed() < 30:
            rig.send("robot_pose", {"mode": "LOCAL_FRONTIER"})
            if sent is None and rig.elapsed() > 6.0:
                sent = survivor(rig, 1)
                sent_at = rig.elapsed()
            if sent is not None and 8.0 < rig.elapsed() < 9.5:
                assert not [r for r in rig.received() if r["event_id"] == sent["event_id"]], \
                    "server knew the report during the blackout"
            reports = [r for r in rig.received() if r["type"] == "survivor_detected"]
            if reports and rig.elapsed() > sent_at + 1:
                break
            time.sleep(0.2)
        time.sleep(1.0)
    finally:
        rig.close()
    reports = [r for r in rig.received() if r["type"] == "survivor_detected"]
    assert len(reports) == 1 and not reports[0].get("duplicate")
    assert reports[0]["captured_at_ms"] == sent["captured_at_ms"]
    proxy_t0 = min(int(r["t_ms"]) for r in csv.DictReader(open(tmp_path / "proxy.csv")))
    assert reports[0]["received_ms"] >= proxy_t0 + 9_000            # not before the blackout ended
    states = [r["to"] for r in csv.DictReader(open(rig.links / "link_state.csv"))]
    assert states[0] in ("ONLINE", "SYNCING") and "OFFLINE" in states     # SYNCING: events queued before the handshake
    after = states[states.index("OFFLINE"):]
    assert after[:3] == ["OFFLINE", "SYNCING", "ONLINE"]
    delivered = [r for r in csv.DictReader(open(rig.links / "delivered.csv")) if r["type"] == "survivor_detected"]
    assert len(delivered) == 1 and int(delivered[0]["acked_ms"]) >= int(delivered[0]["captured_at_ms"])


def test_flapping_link_delivers_every_report_exactly_once(tmp_path):
    # 6 s windows: longer than the 5 s OFFLINE threshold, so each one is a full outage.
    rig = Rig(tmp_path, 7720, "2-8,10-16")
    sent = []
    try:
        n = 0
        while rig.elapsed() < 45:
            rig.send("robot_pose", {"mode": "LOCAL_FRONTIER"})
            if rig.elapsed() > len(sent) * 1.5 and len(sent) < 10:
                n += 1
                sent.append(survivor(rig, n)["event_id"])
            got = {r["event_id"] for r in rig.received() if r["type"] == "survivor_detected"}
            if len(sent) == 10 and set(sent) <= got:
                break
            time.sleep(0.2)
        time.sleep(1.0)
    finally:
        rig.close()
    rows = rig.received()
    reports = [r["event_id"] for r in rows if r["type"] == "survivor_detected" and not r.get("duplicate")]
    assert sorted(reports) == sorted(sent)
    ids = [r["event_id"] for r in rows if not r.get("duplicate")]
    assert len(ids) == len(set(ids))
    states = [r["to"] for r in csv.DictReader(open(rig.links / "link_state.csv"))]
    assert states.count("OFFLINE") >= 2
