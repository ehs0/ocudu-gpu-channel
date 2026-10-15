"""Rescue wire formats: event envelope, datagram packing, map tile codec, stale-task rules."""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "multi_robot_rescue"))

import messages  # noqa: E402


def event(seq=1, type_="robot_pose", payload=None, **kw):
    return messages.make_event(mission_id="m1", robot_id="robot_2", boot="R1", seq=seq, type_=type_,
                               captured_at_ms=1_000 + seq, pose=(1.0, 2.0, 0.1),
                               payload=payload if payload is not None else {"mode": "LOCAL_FRONTIER"}, **kw)


def test_event_round_trip_and_ids():
    e = event(seq=42, type_="survivor_detected",
              payload={"candidate_id": "robot_2-c1", "position_m": [3.0, 4.0]}, confidence=0.9)
    assert e["event_id"] == "robot_2-R1-000042"
    assert messages.validate_event(e) is e
    assert messages.priority(e) == 0
    assert messages.coalesce_key(e) is None
    assert messages.coalesce_key(event()) == "robot_2/robot_pose"


@pytest.mark.parametrize("mutate", [
    lambda e: e.update(schema_version=2),
    lambda e: e.update(type="teleport"),
    lambda e: e.update(event_id="robot_3-R1-000001"),
    lambda e: e.update(seq=0),
    lambda e: e["local_pose"].update(frame="robot_2/map"),
    lambda e: e["local_pose"].update(x=float("nan")),
])
def test_event_rejections(mutate):
    e = event()
    messages.validate_event(dict(e))      # the base is valid
    mutate(e)
    with pytest.raises(ValueError):
        messages.validate_event(e)


def test_survivor_payload_is_checked():
    e = event(type_="survivor_detected", payload={"candidate_id": "c", "position_m": [1.0]})
    with pytest.raises(ValueError):
        messages.validate_event(e)


def test_pack_events_respects_datagram_limit():
    events = [event(seq=i) for i in range(1, 40)]
    data, included = messages.pack_events(events, robot_id="robot_2", session_id="s", sent_ms=5)
    assert len(data) <= messages.MAX_DATAGRAM_BYTES
    assert 1 <= len(included) < len(events)
    assert included == events[:len(included)]
    body = messages.parse_uplink(data)
    assert [e["event_id"] for e in body["events"]] == [e["event_id"] for e in included]


def test_tile_codec_round_trip():
    cells = [i % 3 for i in range(256)]
    assert messages.unpack_cells(messages.pack_cells(cells), 256) == cells
    with pytest.raises(ValueError):
        messages.unpack_cells(messages.pack_cells(cells), 300)


def task(revision=5, issued=10_000, ttl=10_000, robot="robot_2", mission="m1"):
    return messages.make_task(mission_id=mission, robot_id=robot, command_id=f"cmd_{revision}", revision=revision,
                              issued_at_ms=issued, ttl_ms=ttl, target=(1.0, 2.0), map_revision=3)


def test_task_acceptance_rules():
    assert messages.check_task(task(), robot_id="robot_2", mission_id="m1", last_revision=4, now=12_000) is None
    assert messages.check_task(task(), robot_id="robot_2", mission_id="m1", last_revision=4, now=20_000) == "expired"
    assert messages.check_task(task(), robot_id="robot_2", mission_id="m1", last_revision=5, now=12_000) == \
        "old_revision"
    assert messages.check_task(task(robot="robot_1"), robot_id="robot_2", mission_id="m1", last_revision=0,
                               now=12_000) == "wrong_robot"
    assert messages.check_task(task(mission="m0"), robot_id="robot_2", mission_id="m1", last_revision=0,
                               now=12_000) == "wrong_mission"


def test_stale_task_delivered_after_reconnect_is_rejected():
    # Issued before an outage with a 10 s ttl, delivered 25 s later: expired.
    late = task(issued=1_000, ttl=10_000)
    assert messages.check_task(late, robot_id="robot_2", mission_id="m1", last_revision=0, now=26_000) == "expired"
    # Older revision than one already accepted after reconnect: rejected even if still valid.
    assert messages.check_task(task(revision=3, issued=25_000), robot_id="robot_2", mission_id="m1",
                               last_revision=4, now=26_000) == "old_revision"


def test_downlink_task_validation():
    good = messages.downlink("task", robot_id="robot_2", sent_ms=1, task=task())
    assert messages.parse_downlink(good)["task"]["revision"] == 5
    bad = task()
    bad["valid_until_ms"] = bad["issued_at_ms"]
    with pytest.raises(ValueError):
        messages.parse_downlink(messages.downlink("task", robot_id="robot_2", sent_ms=1, task=bad))
