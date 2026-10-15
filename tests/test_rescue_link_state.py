"""UE link state machine: thresholds, hysteresis, resume handshake, probe backoff (injected clock)."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "multi_robot_rescue"))

from ue_link import LinkStateMachine, RttEstimator  # noqa: E402


def machine():
    return LinkStateMachine(degraded_timeout_s=3.0, offline_timeout_s=5.0, recover_replies=2,
                            heartbeat_interval_s=1.0, backoff_initial_s=1.0, backoff_max_s=8.0, now=0.0)


def test_boot_handshake_goes_online_or_offline():
    sm = machine()
    sm.on_resume_ack(0.5, backlog=0)
    assert sm.state == "ONLINE"
    sm = machine()
    sm.tick(4.9, 0)
    assert sm.state == "BOOT"
    sm.tick(5.0, 0)
    assert sm.state == "OFFLINE"


def test_reply_timeouts_degrade_then_offline():
    sm = machine()
    sm.on_resume_ack(0.0, backlog=0)
    sm.tick(2.9, 0)
    assert sm.state == "ONLINE"
    sm.tick(3.0, 0)
    assert sm.state == "DEGRADED"
    sm.tick(5.0, 0)
    assert sm.state == "OFFLINE"
    assert [t[2] for t in sm.transitions] == ["ONLINE", "DEGRADED", "OFFLINE"]


def test_degraded_needs_sustained_replies():
    sm = machine()
    sm.on_resume_ack(0.0, backlog=0)
    sm.tick(3.5, 0)
    sm.on_reply(3.6)
    assert sm.state == "DEGRADED"          # one reply is not enough
    sm.on_reply(4.6)
    assert sm.state == "ONLINE"


def test_offline_recovers_through_syncing_until_backlog_acked():
    sm = machine()
    sm.on_resume_ack(0.0, backlog=0)
    sm.tick(6.0, 0)
    assert sm.state == "OFFLINE"
    sm.on_reply(7.0)                       # a late heartbeat ACK does not bring it back
    assert sm.state == "OFFLINE"
    sm.on_resume_ack(8.0, backlog=12)
    assert sm.state == "SYNCING"
    sm.tick(9.0, 3)
    assert sm.state == "SYNCING"
    sm.tick(9.5, 0)
    assert sm.state == "ONLINE"


def test_syncing_falls_back_offline_if_the_link_drops_again():
    sm = machine()
    sm.tick(5.0, 0)
    sm.on_resume_ack(6.0, backlog=5)
    sm.tick(11.0, 5)
    assert sm.state == "OFFLINE"


def test_probe_backoff_is_bounded_and_resets():
    sm = machine()
    sm.tick(5.0, 0)
    assert sm.state == "OFFLINE"
    times, t = [], 5.0
    while t < 40.0:
        if sm.probe_due(t) == "resume_hello":
            times.append(t)
        t = round(t + 0.1, 1)
    gaps = [round(b - a, 1) for a, b in zip(times, times[1:])]
    assert gaps[:4] == [1.0, 2.0, 4.0, 8.0]
    assert max(gaps) == 8.0                 # capped, and it never stops probing
    sm.on_resume_ack(40.0, backlog=0)
    assert sm.state == "ONLINE"
    assert sm.backoff_s == 1.0
    assert sm.probe_due(sm.next_probe) == "heartbeat"


def test_hard_disconnect():
    sm = machine()
    sm.on_resume_ack(0.0, backlog=0)
    sm.on_hard_disconnect(1.0, "socket_lost")
    assert sm.state == "OFFLINE"


def test_rto_tracks_rtt():
    rtt = RttEstimator()
    assert rtt.rto_ms == 1500
    for _ in range(20):
        rtt.sample(40.0)
    assert 400 <= rtt.rto_ms <= 500
    for _ in range(20):
        rtt.sample(3000.0)
    assert 3000 < rtt.rto_ms <= 6000
    for _ in range(50):
        rtt.sample(9000.0)
    assert rtt.rto_ms == 6000                          # capped
