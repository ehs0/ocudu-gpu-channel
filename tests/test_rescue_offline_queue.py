"""Offline queue: priority order, coalescing, eviction policy, ACK timing, persistence."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "multi_robot_rescue"))

import messages  # noqa: E402
from offline_queue import OfflineQueue  # noqa: E402


def ev(seq, type_, payload=None, mission="m1"):
    if payload is None:
        payload = {"candidate_id": f"c{seq}", "position_m": [1.0, 2.0]} if type_ == "survivor_detected" else {}
    if type_ == "map_tile" and not payload:
        payload = {"tile": [seq % 3, 0], "size": 16, "cells": messages.pack_cells([1] * 256)}
    return messages.make_event(mission_id=mission, robot_id="robot_2", boot="R1", seq=seq, type_=type_,
                               captured_at_ms=100 + seq, pose=(0.0, 0.0, 0.0), payload=payload)


def put(q, e, now=1000):
    return q.put(e, priority=messages.priority(e), coalesce_key=messages.coalesce_key(e), now_ms=now)


def test_priority_then_sequence(tmp_path):
    q = OfflineQueue(tmp_path / "q.db", max_bytes=1 << 20)
    put(q, ev(1, "robot_pose"))
    put(q, ev(2, "map_tile"))
    put(q, ev(3, "survivor_detected"))
    put(q, ev(4, "app_link_state"))
    order = [e["type"] for e in q.next_batch(now_ms=2000, rto_ms=500, limit=10)]
    assert order == ["survivor_detected", "map_tile", "robot_pose", "app_link_state"]


def test_coalescing_keeps_newest_per_key(tmp_path):
    q = OfflineQueue(tmp_path / "q.db", max_bytes=1 << 20)
    for seq in (1, 2, 3):
        put(q, ev(seq, "robot_pose"))
    put(q, ev(4, "map_tile", {"tile": [1, 1], "size": 16, "cells": messages.pack_cells([1] * 256)}))
    put(q, ev(5, "map_tile", {"tile": [1, 1], "size": 16, "cells": messages.pack_cells([2] * 256)}))
    put(q, ev(6, "map_tile", {"tile": [2, 1], "size": 16, "cells": messages.pack_cells([2] * 256)}))
    seqs = sorted(e["seq"] for e in q.next_batch(now_ms=2000, rto_ms=500, limit=10))
    assert seqs == [3, 5, 6]


def test_in_flight_events_wait_for_the_rto(tmp_path):
    q = OfflineQueue(tmp_path / "q.db", max_bytes=1 << 20)
    put(q, ev(1, "survivor_detected"))
    first = q.next_batch(now_ms=1000, rto_ms=500, limit=10)
    q.mark_sent([e["event_id"] for e in first], 1000)
    assert q.next_batch(now_ms=1200, rto_ms=500, limit=10) == []
    assert q.in_flight(now_ms=1200, rto_ms=500) == 1
    assert [e["seq"] for e in q.next_batch(now_ms=1600, rto_ms=500, limit=10)] == [1]


def test_ack_removes_and_reports_timing(tmp_path):
    q = OfflineQueue(tmp_path / "q.db", max_bytes=1 << 20)
    e = ev(7, "survivor_detected")
    put(q, e, now=900)
    q.mark_sent([e["event_id"]], 1000)
    q.mark_sent([e["event_id"]], 2000)
    (d,) = q.ack([e["event_id"], "robot_2-R1-999999"], 2100)
    assert (d.captured_at_ms, d.enqueued_ms, d.first_send_ms, d.acked_ms, d.send_count) == (107, 900, 1000, 2100, 2)
    assert q.count() == 0
    assert q.ack([e["event_id"]], 2200) == []          # a duplicate ACK matches nothing


def test_eviction_never_drops_survivor_reports(tmp_path):
    q = OfflineQueue(tmp_path / "q.db", max_bytes=6000)
    put(q, ev(1, "survivor_detected"))
    for seq in range(2, 80):
        put(q, ev(seq, "map_tile", {"tile": [seq, 0], "size": 16, "cells": messages.pack_cells([1] * 256)}))
    put(q, ev(80, "survivor_detected"))
    types = q.counts_by_type()
    assert types["survivor_detected"] == 2
    assert q.bytes() <= 6000
    assert q.evicted > 0
    # Only survivor reports left over budget: they are kept, not evicted.
    small = OfflineQueue(tmp_path / "s.db", max_bytes=10)
    put(small, ev(1, "survivor_detected"))
    assert small.count() == 1 and small.evicted == 0


def test_queue_survives_restart(tmp_path):
    q = OfflineQueue(tmp_path / "q.db", max_bytes=1 << 20)
    put(q, ev(1, "survivor_detected"))
    put(q, ev(2, "robot_pose"))
    q.close()
    again = OfflineQueue(tmp_path / "q.db", max_bytes=1 << 20)
    assert again.count() == 2
    assert again.next_batch(now_ms=0, rto_ms=1, limit=1)[0]["type"] == "survivor_detected"


def test_purge_other_missions(tmp_path):
    q = OfflineQueue(tmp_path / "q.db", max_bytes=1 << 20)
    put(q, ev(1, "robot_pose", mission="old"))
    put(q, ev(2, "survivor_detected", mission="new"))
    assert q.purge_other_missions("new") == 1
    assert q.count() == 1
