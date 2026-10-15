"""Wire formats of the rescue demo (stdlib only; `ue_link.py` runs on any python3).

Four planes:

1. **Application plane** (robot's UE link <-> mission server, UDP over the 5G
   user plane in the live gate): one JSON object per datagram, at most
   `MAX_DATAGRAM_BYTES`. Uplink `kind`: `data` (a batch of events),
   `heartbeat`, `resume_hello`. Downlink `kind`: `ack`, `heartbeat_ack`,
   `resume_ack`, `task`.
2. **Robot bus** (robot.py <-> its ue_link.py, unix datagram, on the robot):
   `{"kind": "event", "event": {...}}` up; `{"kind": "task", ...}` and
   `{"kind": "link_state", ...}` down. Not radio transported.
3. **Body bus** (sim.py <-> robot.py, unix datagram, on the robot): `sensor`
   frames down (odometry, 2-D scan, perception-mock detections), `cmd` up
   (body velocity). Not radio transported.
4. **Positions feed** (sim.py -> Sionna bridge, dashboard; ZMQ PUB): the
   bridge's `{"event": "positions", "frame": "arena", "nodes": ...}` message
   (`scripts/sionna_rt/run_bridge.py`, `ExternalPositionSource`) with each
   robot's antenna as its UE node, plus `robots` and a `referee` object the
   bridge ignores. The referee object carries ground truth (survivors) for the
   dashboard's referee overlay only; neither robot.py nor mission_server.py
   subscribes to this feed.

Events (application plane) share one envelope; `event_id` is
`<robot_id>-<boot>-<seq>` and unique per robot process lifetime. Priority 0 is
the most urgent and is never evicted from the offline queue.
"""

from __future__ import annotations

import base64
import json
import math
import time
from typing import Any

SCHEMA_VERSION = 1
MAX_DATAGRAM_BYTES = 1200       # inside one IPv4 MTU over the srsUE TUN

# type -> (priority, coalesce): a coalescing type keeps only the newest unsent
# event per coalesce key (pose: per robot; map tile: per tile).
EVENT_TYPES = {
    "survivor_detected": (0, False),
    "mission_task_ack": (1, False),
    "safety_stop": (1, False),
    "map_tile": (2, True),
    "robot_pose": (3, True),
    "app_link_state": (3, False),
}

UPLINK_KINDS = ("data", "heartbeat", "resume_hello")
DOWNLINK_KINDS = ("ack", "heartbeat_ack", "resume_ack", "task")
TASK_TYPES = ("explore_frontier",)

LINK_STATES = ("BOOT", "ONLINE", "DEGRADED", "OFFLINE", "SYNCING")


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def _finite(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{where} must be a finite number")
    return float(value)


def _int(value: Any, where: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{where} must be an integer >= {minimum}")
    return value


def _str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where} must be a non-empty string")
    return value


def dumps(body: dict) -> bytes:
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def loads(data: bytes) -> dict:
    try:
        body = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"not JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError("not a JSON object")
    return body


# --------------------------------------------------------------------------- events

def make_event(*, mission_id: str, robot_id: str, boot: str, seq: int, type_: str,
               captured_at_ms: int, pose: tuple[float, float, float] | None,
               payload: dict, confidence: float | None = None) -> dict:
    if type_ not in EVENT_TYPES:
        raise ValueError(f"unknown event type {type_!r}")
    event = {
        "schema_version": SCHEMA_VERSION,
        "mission_id": mission_id,
        "robot_id": robot_id,
        "event_id": f"{robot_id}-{boot}-{seq:06d}",
        "seq": seq,
        "type": type_,
        "captured_at_ms": captured_at_ms,
        "payload": payload,
    }
    if pose is not None:
        event["local_pose"] = {"frame": "site", "x": round(pose[0], 3), "y": round(pose[1], 3),
                               "yaw": round(pose[2], 4)}
    if confidence is not None:
        event["confidence"] = round(confidence, 3)
    return event


def coalesce_key(event: dict) -> str | None:
    """Key under which a newer unsent event replaces an older one, or None."""

    priority, coalesce = EVENT_TYPES[event["type"]]
    if not coalesce:
        return None
    if event["type"] == "map_tile":
        tx, ty = event["payload"]["tile"]
        return f"{event['robot_id']}/tile/{tx}/{ty}"
    return f"{event['robot_id']}/{event['type']}"


def priority(event: dict) -> int:
    return EVENT_TYPES[event["type"]][0]


def validate_event(event: Any) -> dict:
    """Raise ValueError unless `event` is a well-formed envelope; return it."""

    if not isinstance(event, dict):
        raise ValueError("event must be an object")
    if event.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
    for key in ("mission_id", "robot_id", "event_id", "type"):
        _str(event.get(key), key)
    if event["type"] not in EVENT_TYPES:
        raise ValueError(f"unknown event type {event['type']!r}")
    if not event["event_id"].startswith(event["robot_id"] + "-"):
        raise ValueError("event_id must start with robot_id")
    _int(event.get("seq"), "seq", 1)
    _int(event.get("captured_at_ms"), "captured_at_ms", 1)
    if not isinstance(event.get("payload"), dict):
        raise ValueError("payload must be an object")
    pose = event.get("local_pose")
    if pose is not None:
        if not isinstance(pose, dict) or pose.get("frame") != "site":
            raise ValueError("local_pose must be an object in frame 'site'")
        for key in ("x", "y", "yaw"):
            _finite(pose.get(key), f"local_pose.{key}")
    payload = event["payload"]
    if event["type"] == "survivor_detected":
        _str(payload.get("candidate_id"), "payload.candidate_id")
        position = payload.get("position_m")
        if not (isinstance(position, list) and len(position) == 2):
            raise ValueError("payload.position_m must be [x, y]")
        for index, value in enumerate(position):
            _finite(value, f"payload.position_m[{index}]")
    elif event["type"] == "map_tile":
        tile = payload.get("tile")
        if not (isinstance(tile, list) and len(tile) == 2):
            raise ValueError("payload.tile must be [tx, ty]")
        _int(tile[0], "payload.tile[0]")
        _int(tile[1], "payload.tile[1]")
        _int(payload.get("size"), "payload.size", 1)
        _str(payload.get("cells"), "payload.cells")
    elif event["type"] == "mission_task_ack":
        _str(payload.get("command_id"), "payload.command_id")
        if not isinstance(payload.get("accepted"), bool):
            raise ValueError("payload.accepted must be a boolean")
    return event


# --------------------------------------------------------------------------- application datagrams

def uplink(kind: str, *, robot_id: str, session_id: str, sent_ms: int, **fields) -> bytes:
    if kind not in UPLINK_KINDS:
        raise ValueError(f"unknown uplink kind {kind!r}")
    data = dumps({"v": SCHEMA_VERSION, "kind": kind, "robot_id": robot_id, "session_id": session_id,
                  "sent_ms": sent_ms, **fields})
    if len(data) > MAX_DATAGRAM_BYTES:
        raise ValueError(f"datagram is {len(data)} bytes, limit {MAX_DATAGRAM_BYTES}")
    return data


def downlink(kind: str, *, robot_id: str, sent_ms: int, **fields) -> bytes:
    if kind not in DOWNLINK_KINDS:
        raise ValueError(f"unknown downlink kind {kind!r}")
    data = dumps({"v": SCHEMA_VERSION, "kind": kind, "robot_id": robot_id, "sent_ms": sent_ms, **fields})
    if len(data) > MAX_DATAGRAM_BYTES:
        raise ValueError(f"datagram is {len(data)} bytes, limit {MAX_DATAGRAM_BYTES}")
    return data


def pack_events(events: list[dict], *, robot_id: str, session_id: str, sent_ms: int) -> tuple[bytes, list[dict]]:
    """Fit as many leading `events` as possible into one `data` datagram.

    Returns (datagram, events included). Raises if even the first event does not fit.
    """

    included: list[dict] = []
    data = b""
    for event in events:
        candidate = dumps({"v": SCHEMA_VERSION, "kind": "data", "robot_id": robot_id,
                           "session_id": session_id, "sent_ms": sent_ms, "events": included + [event]})
        if len(candidate) > MAX_DATAGRAM_BYTES:
            break
        included.append(event)
        data = candidate
    if not included:
        raise ValueError(f"event {events[0].get('event_id')} alone exceeds {MAX_DATAGRAM_BYTES} bytes")
    return data, included


def parse_uplink(data: bytes) -> dict:
    body = loads(data)
    if body.get("v") != SCHEMA_VERSION or body.get("kind") not in UPLINK_KINDS:
        raise ValueError("not an uplink datagram")
    _str(body.get("robot_id"), "robot_id")
    _str(body.get("session_id"), "session_id")
    _int(body.get("sent_ms"), "sent_ms", 1)
    if body["kind"] == "data":
        events = body.get("events")
        if not isinstance(events, list) or not events:
            raise ValueError("data datagram without events")
    return body


def parse_downlink(data: bytes) -> dict:
    body = loads(data)
    if body.get("v") != SCHEMA_VERSION or body.get("kind") not in DOWNLINK_KINDS:
        raise ValueError("not a downlink datagram")
    _str(body.get("robot_id"), "robot_id")
    _int(body.get("sent_ms"), "sent_ms", 1)
    if body["kind"] == "task":
        validate_task(body.get("task"))
    return body


def make_task(*, mission_id: str, robot_id: str, command_id: str, revision: int, issued_at_ms: int,
              ttl_ms: int, target: tuple[float, float], map_revision: int) -> dict:
    return {
        "mission_id": mission_id,
        "command_id": command_id,
        "robot_id": robot_id,
        "revision": revision,
        "issued_at_ms": issued_at_ms,
        "valid_until_ms": issued_at_ms + ttl_ms,
        "task": "explore_frontier",
        "target_pose_m": {"x": round(target[0], 2), "y": round(target[1], 2)},
        "map_revision": map_revision,
        "priority": "normal",
    }


def validate_task(task: Any) -> dict:
    if not isinstance(task, dict):
        raise ValueError("task must be an object")
    for key in ("mission_id", "command_id", "robot_id"):
        _str(task.get(key), f"task.{key}")
    _int(task.get("revision"), "task.revision", 1)
    issued = _int(task.get("issued_at_ms"), "task.issued_at_ms", 1)
    until = _int(task.get("valid_until_ms"), "task.valid_until_ms", 1)
    if until <= issued:
        raise ValueError("task.valid_until_ms must be after issued_at_ms")
    if task.get("task") not in TASK_TYPES:
        raise ValueError(f"task.task must be one of {TASK_TYPES}")
    target = task.get("target_pose_m")
    if not isinstance(target, dict):
        raise ValueError("task.target_pose_m must be an object")
    _finite(target.get("x"), "task.target_pose_m.x")
    _finite(target.get("y"), "task.target_pose_m.y")
    return task


def check_task(task: dict, *, robot_id: str, mission_id: str, last_revision: int, now: int) -> str | None:
    """Why the robot must reject `task` now, or None to accept it."""

    if task["robot_id"] != robot_id:
        return "wrong_robot"
    if task["mission_id"] != mission_id:
        return "wrong_mission"
    if task["revision"] <= last_revision:
        return "old_revision"
    if now >= task["valid_until_ms"]:
        return "expired"
    return None


# --------------------------------------------------------------------------- map tiles

# Tri-state cell codes shared by robot and server grids.
UNKNOWN, FREE, OCCUPIED = 0, 1, 2


def pack_cells(cells: list[int]) -> str:
    """2 bits per cell, little-end first, base64."""

    out = bytearray((len(cells) + 3) // 4)
    for index, value in enumerate(cells):
        out[index >> 2] |= (value & 3) << ((index & 3) * 2)
    return base64.b64encode(bytes(out)).decode("ascii")


def unpack_cells(text: str, count: int) -> list[int]:
    raw = base64.b64decode(text.encode("ascii"), validate=True)
    if len(raw) != (count + 3) // 4:
        raise ValueError(f"tile has {len(raw)} bytes, expected {(count + 3) // 4}")
    return [(raw[index >> 2] >> ((index & 3) * 2)) & 3 for index in range(count)]


# --------------------------------------------------------------------------- positions feed

def positions_message(*, t_unix_ms: int, sim_time_s: float,
                      radio_nodes: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]],
                      robots: dict[str, dict], referee: dict) -> str:
    return json.dumps({
        "event": "positions",
        "t_unix_ms": t_unix_ms,
        "frame": "arena",
        "sim_time_s": round(sim_time_s, 3),
        "nodes": {
            node: {"position_m": [round(v, 3) for v in position],
                   "velocity_mps": [round(v, 3) for v in velocity]}
            for node, (position, velocity) in radio_nodes.items()
        },
        "robots": robots,
        "referee": referee,
    }, separators=(",", ":"))
