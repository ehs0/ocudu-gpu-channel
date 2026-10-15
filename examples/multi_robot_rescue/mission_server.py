#!/usr/bin/env python3
"""Central mission server: fused map, robot status, survivor registry, task allocation (numpy).

It knows only what arrives on its UDP socket. In the live gate that socket is
bound to the UPF-side address (10.45.1.1), so every byte crossed the robot's
UE, the channel emulator and the gNB. It does not subscribe to the positions
feed and reads nothing from the world simulator (`tests/test_rescue_isolation.py`).

Per datagram: `heartbeat` -> `heartbeat_ack`, `resume_hello` -> `resume_ack`,
`data` -> one `ack` listing every event id of the datagram — new, duplicate
(deduplicated by event id) or from another mission (acknowledged, discarded,
counted). The ACK is the delivery confirmation the UE link waits for.

- **Map fusion**: per robot the newest tile (by capture time) is kept; the
  fused map overlays every robot's known cells, newer captures on top. An
  older tile arriving late never replaces a newer one of the same robot.
- **Robot status** (server's view): ONLINE / DEGRADED / OFFLINE from the age of
  the last datagram (same thresholds as the UE link), SYNCING while the robot
  reports it is draining a backlog. Offline robots keep their last pose, its
  age and their last task; they are sent nothing.
- **Survivors**: reports within `--merge-radius-m` of an existing record are
  merged into it; each record keeps the capture time on the robot and the
  first receive time here — two different timestamps.
- **Tasks** (`planner.allocate`): every `--plan-period-s`, ONLINE robots with a
  fresh pose get a target; a task carries a revision and expires after
  `--task-ttl-s`. `--no-planner` turns the central tasks off (ablation).

Outputs (`--out`): `server.jsonl` (every event received), `tasks_tx.csv`,
`robot_status.csv` (1 Hz), `final_state.json`; `--state-json` is rewritten
twice a second (dashboard, referee).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import pathlib
import select
import signal
import socket
import sys
import time

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import grid  # noqa: E402
import layout  # noqa: E402
import messages  # noqa: E402
import planner  # noqa: E402


class FusedMap:
    def __init__(self):
        self.per_robot: dict[str, np.ndarray] = {}
        self.tile_time: dict[tuple[str, int, int], int] = {}
        self.revision = 0
        self._fused = np.zeros((grid.ROWS, grid.COLS), dtype=np.uint8)
        self._dirty = False

    def apply(self, robot_id: str, payload: dict, captured_at_ms: int) -> bool:
        tx, ty = payload["tile"]
        key = (robot_id, tx, ty)
        if self.tile_time.get(key, -1) >= captured_at_ms:
            return False             # an older view of this tile than we already hold
        state = self.per_robot.setdefault(robot_id, np.zeros((grid.ROWS, grid.COLS), dtype=np.uint8))
        grid.apply_tile(state, payload)
        self.tile_time[key] = captured_at_ms
        self._dirty = True
        return True

    def fused(self) -> np.ndarray:
        if self._dirty:
            out = np.zeros((grid.ROWS, grid.COLS), dtype=np.uint8)
            order = []
            for (rid, tx, ty), t in self.tile_time.items():
                order.append((t, rid, tx, ty))
            for _, rid, tx, ty in sorted(order):
                s = grid.TILE_CELLS
                src = self.per_robot[rid][ty * s:(ty + 1) * s, tx * s:(tx + 1) * s]
                dst = out[ty * s:(ty + 1) * s, tx * s:(tx + 1) * s]
                known = src != grid.UNKNOWN
                dst[known] = src[known]
            self._fused = out
            self._dirty = False
            self.revision += 1
        return self._fused


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bind", required=True, help="ip:port (the UPF-side address in the live gate)")
    ap.add_argument("--mission-id", required=True)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--state-json", type=pathlib.Path, required=True)
    ap.add_argument("--task-ttl-s", type=float, default=10.0)
    ap.add_argument("--plan-period-s", type=float, default=2.0)
    ap.add_argument("--degraded-timeout-s", type=float, default=3.0)
    ap.add_argument("--offline-timeout-s", type=float, default=5.0)
    ap.add_argument("--pose-fresh-s", type=float, default=5.0)
    ap.add_argument("--merge-radius-m", type=float, default=1.5)
    ap.add_argument("--avoid-s", type=float, default=60.0, help="how long a refused target is not re-offered")
    ap.add_argument("--no-planner", action="store_true")
    ap.add_argument("--max-seconds", type=float, default=3600.0)
    args = ap.parse_args(argv)

    host, _, port = args.bind.rpartition(":")
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind((host, int(port)))
    udp.setblocking(False)
    args.out.mkdir(parents=True, exist_ok=True)
    events_log = open(args.out / "server.jsonl", "w", encoding="utf-8")
    tasks_file = open(args.out / "tasks_tx.csv", "w", newline="", encoding="utf-8")
    tasks_csv = csv.writer(tasks_file)
    tasks_csv.writerow(["sent_ms", "robot_id", "command_id", "revision", "target_x", "target_y", "valid_until_ms",
                        "resend"])
    status_file = open(args.out / "robot_status.csv", "w", newline="", encoding="utf-8")
    status_csv = csv.writer(status_file)
    status_csv.writerow(["t_ms", "robot_id", "status", "reported_state", "last_heard_age_s", "pose_age_s",
                         "backlog", "x", "y"])

    mono = time.monotonic
    robots: dict[str, dict] = {}
    seen: set[str] = set()
    fused = FusedMap()
    survivors: list[dict] = []
    counters = {"datagrams_rx": 0, "datagrams_invalid": 0, "events_new": 0, "events_duplicate": 0,
                "events_other_mission": 0, "events_invalid": 0, "acks_tx": 0, "tasks_tx": 0, "task_resends": 0,
                "tiles_stale": 0}
    command_seq = 0
    stop = False

    def on_signal(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    def robot(rid: str) -> dict:
        return robots.setdefault(rid, {"addr": None, "session_id": None, "last_heard": None,
                                       "last_heard_ms": None, "reported_state": None, "backlog": None,
                                       "pose": None, "pose_captured_ms": None, "pose_received": None,
                                       "task": None, "task_sent": None, "task_acked": None, "revision": 0,
                                       "events": 0, "mode": None, "goal_m": None, "avoid": []})

    def status_of(info: dict, now: float) -> str:
        if info["last_heard"] is None:
            return "UNKNOWN"
        age = now - info["last_heard"]
        if age >= args.offline_timeout_s:
            return "OFFLINE"
        if age >= args.degraded_timeout_s:
            return "DEGRADED"
        if info["reported_state"] in ("SYNCING", "BOOT", "OFFLINE"):
            return "SYNCING"
        return "ONLINE"

    def send(rid: str, data: bytes) -> None:
        addr = robots[rid]["addr"]
        if addr is not None:
            try:
                udp.sendto(data, addr)
            except OSError:
                pass

    def handle_event(rid: str, event: dict, received_ms: int, now: float) -> None:
        info = robot(rid)
        event_id = event["event_id"]
        if event["mission_id"] != args.mission_id:
            counters["events_other_mission"] += 1
            events_log.write(json.dumps({"received_ms": received_ms, "robot_id": rid, "event_id": event_id,
                                         "type": event["type"], "other_mission": event["mission_id"]}) + "\n")
            return
        if event_id in seen:
            counters["events_duplicate"] += 1
            events_log.write(json.dumps({"received_ms": received_ms, "robot_id": rid, "event_id": event_id,
                                         "type": event["type"], "duplicate": True}) + "\n")
            return
        seen.add(event_id)
        counters["events_new"] += 1
        info["events"] += 1
        record = {"received_ms": received_ms, "robot_id": rid, "event_id": event_id, "type": event["type"],
                  "seq": event["seq"], "captured_at_ms": event["captured_at_ms"],
                  "age_ms": received_ms - event["captured_at_ms"]}
        t = event["type"]
        payload = event["payload"]
        if t == "map_tile":
            try:
                applied = fused.apply(rid, payload, event["captured_at_ms"])
            except ValueError as exc:
                applied = False
                record["error"] = str(exc)
            if not applied:
                counters["tiles_stale"] += 1
            record["tile"] = payload["tile"]
            record["applied"] = applied
        elif t == "robot_pose":
            if info["pose_captured_ms"] is None or event["captured_at_ms"] > info["pose_captured_ms"]:
                pose = event["local_pose"]
                info["pose"] = (pose["x"], pose["y"], pose["yaw"])
                info["pose_captured_ms"] = event["captured_at_ms"]
                info["pose_received"] = now
                info["mode"] = payload.get("mode")
                info["goal_m"] = payload.get("goal_m")
        elif t == "survivor_detected":
            x, y = payload["position_m"]
            report = {"robot_id": rid, "candidate_id": payload["candidate_id"], "event_id": event_id,
                      "captured_at_ms": event["captured_at_ms"], "received_at_ms": received_ms,
                      "position_m": [x, y], "confidence": event.get("confidence")}
            match = next((s for s in survivors
                          if math.hypot(s["position_m"][0] - x, s["position_m"][1] - y) < args.merge_radius_m), None)
            if match is None:
                match = {"survivor_id": f"S{len(survivors) + 1}", "position_m": [x, y], "first_robot_id": rid,
                         "captured_at_ms": event["captured_at_ms"], "received_at_ms": received_ms, "reports": []}
                survivors.append(match)
            match["captured_at_ms"] = min(match["captured_at_ms"], event["captured_at_ms"])
            match["reports"].append(report)
            record["survivor_id"] = match["survivor_id"]
            record["position_m"] = [x, y]
        elif t == "mission_task_ack":
            if info["task"] is not None and payload["command_id"] == info["task"]["command_id"]:
                info["task_acked"] = {"accepted": payload["accepted"], "reason": payload.get("reason"),
                                      "received_ms": received_ms}
                if not payload["accepted"] and payload.get("reason") in ("stuck", "unreachable", "blacklisted"):
                    target = info["task"]["target_pose_m"]
                    info["avoid"].append((target["x"], target["y"], now + args.avoid_s))
                    info["task"] = None        # do not keep resending a refused target
            record["ack"] = payload
        elif t == "app_link_state":
            record["link"] = payload
        events_log.write(json.dumps(record, separators=(",", ":")) + "\n")

    def publish_state(now: float) -> None:
        state = fused.fused()
        body = {
            "t_ms": messages.now_ms(), "mission_id": args.mission_id, "map_revision": fused.revision,
            "map": grid.pack_state(state), "grid": {"rows": grid.ROWS, "cols": grid.COLS, "res_m": grid.RESOLUTION_M,
                                                    "x0": grid.X0, "y0": grid.Y0},
            "known_cells": int((state != grid.UNKNOWN).sum()),
            "survivors": [{k: s[k] for k in ("survivor_id", "position_m", "first_robot_id", "captured_at_ms",
                                             "received_at_ms")} | {"reports": len(s["reports"])} for s in survivors],
            "robots": {rid: {"status": status_of(info, now), "reported_state": info["reported_state"],
                             "last_heard_age_s": None if info["last_heard"] is None
                             else round(now - info["last_heard"], 2),
                             "pose": info["pose"], "pose_age_s": None if info["pose_received"] is None
                             else round(now - info["pose_received"], 2),
                             "backlog": info["backlog"], "mode": info["mode"], "goal_m": info["goal_m"],
                             "events": info["events"],
                             "task": None if info["task"] is None else {
                                 k: info["task"][k] for k in ("command_id", "revision", "valid_until_ms",
                                                              "target_pose_m")},
                             "task_acked": info["task_acked"]}
                       for rid, info in sorted(robots.items())},
            "counters": counters, "planner": "off" if args.no_planner else "deterministic_frontier_allocator",
        }
        tmp = args.state_json.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, args.state_json)

    def issue(rid: str, target: tuple[float, float], wall: int) -> None:
        nonlocal command_seq
        info = robots[rid]
        command_seq += 1
        info["revision"] += 1
        task = messages.make_task(mission_id=args.mission_id, robot_id=rid, command_id=f"cmd_{command_seq:05d}",
                                  revision=info["revision"], issued_at_ms=wall, ttl_ms=int(args.task_ttl_s * 1000),
                                  target=target, map_revision=fused.revision)
        info["task"], info["task_sent"], info["task_acked"] = task, wall, None
        counters["tasks_tx"] += 1
        send(rid, messages.downlink("task", robot_id=rid, sent_ms=wall, task=task))
        tasks_csv.writerow([wall, rid, task["command_id"], task["revision"], target[0], target[1],
                            task["valid_until_ms"], 0])

    def still_frontier(frontier, target) -> bool:
        r, c = grid.cell_of(target["x"], target["y"])
        k = int(round(1.0 / grid.RESOLUTION_M))
        return bool(frontier[max(r - k, 0):r + k + 1, max(c - k, 0):c + k + 1].any())

    def plan(now: float) -> None:
        state = fused.fused()
        frontier = grid.frontier_mask(state)
        wall = messages.now_ms()
        commandable, reserved = {}, []
        for rid, info in robots.items():
            status = status_of(info, now)
            fresh = info["pose_received"] is not None and now - info["pose_received"] < args.pose_fresh_s
            task = info["task"]
            if status in ("OFFLINE", "DEGRADED"):
                if info["pose"] is not None:
                    reserved.append(info["pose"][:2])
                continue
            if status != "ONLINE" or not fresh:
                continue
            refused = info["task_acked"] is not None and not info["task_acked"]["accepted"]
            if (task is not None and wall < task["valid_until_ms"] and not refused
                    and still_frontier(frontier, task["target_pose_m"])):
                # Commitment: a live task whose target is still a frontier is not replaced.
                target = (task["target_pose_m"]["x"], task["target_pose_m"]["y"])
                reserved.append(target)
                if wall > task["valid_until_ms"] - 3000:
                    issue(rid, target, wall)                       # renew: same target, new revision
                elif info["task_acked"] is None and wall - info["task_sent"] > 2000:
                    counters["task_resends"] += 1                  # same command again; the robot dedups it
                    send(rid, messages.downlink("task", robot_id=rid, sent_ms=wall, task=task))
                    info["task_sent"] = wall
                    tasks_csv.writerow([wall, rid, task["command_id"], task["revision"], target[0], target[1],
                                        task["valid_until_ms"], 1])
                continue
            info["avoid"] = [a for a in info["avoid"] if a[2] > now]
            commandable[rid] = {"pose": info["pose"][:2], "avoid": [(a[0], a[1]) for a in info["avoid"]]}
        for rid, target in planner.allocate(state, commandable, reserved).items():
            issue(rid, target, wall)
        tasks_file.flush()

    print(f"{messages.now_ms()} event=mission_server_ready bind={args.bind} mission={args.mission_id}", flush=True)
    start = mono()
    last_plan = last_state = last_status = 0.0
    while not stop and mono() - start < args.max_seconds:
        try:
            ready, _, _ = select.select([udp], [], [], 0.05)
        except InterruptedError:
            continue
        now = mono()
        while ready:
            try:
                data, addr = udp.recvfrom(65536)
            except (BlockingIOError, InterruptedError):
                break
            counters["datagrams_rx"] += 1
            received_ms = messages.now_ms()
            try:
                body = messages.parse_uplink(data)
            except ValueError:
                counters["datagrams_invalid"] += 1
                continue
            rid = body["robot_id"]
            if rid not in layout.ROBOT_IDS:
                counters["datagrams_invalid"] += 1
                continue
            info = robot(rid)
            info["addr"], info["last_heard"], info["last_heard_ms"] = addr, now, received_ms
            info["session_id"] = body["session_id"]
            kind = body["kind"]
            if kind in ("heartbeat", "resume_hello"):
                info["reported_state"] = body.get("state")
                info["backlog"] = body.get("backlog")
                reply = "heartbeat_ack" if kind == "heartbeat" else "resume_ack"
                send(rid, messages.downlink(reply, robot_id=rid, sent_ms=received_ms, echo_ms=body["sent_ms"],
                                            mission_id=args.mission_id))
                continue
            ids = []
            for event in body["events"]:
                try:
                    messages.validate_event(event)
                    if event["robot_id"] != rid:
                        raise ValueError("event robot_id differs from the datagram's")
                except (ValueError, KeyError, TypeError):
                    counters["events_invalid"] += 1
                    continue
                handle_event(rid, event, received_ms, now)
                ids.append(event["event_id"])
            # One ACK per datagram, split if the id list would not fit.
            while ids:
                chunk, ids = ids[:30], ids[30:]
                send(rid, messages.downlink("ack", robot_id=rid, sent_ms=messages.now_ms(), event_ids=chunk))
                counters["acks_tx"] += 1
        events_log.flush()
        if not args.no_planner and now - last_plan >= args.plan_period_s:
            last_plan = now
            plan(now)
        if now - last_state >= 0.5:
            last_state = now
            publish_state(now)
        if now - last_status >= 1.0:
            last_status = now
            wall = messages.now_ms()
            for rid, info in sorted(robots.items()):
                pose = info["pose"] or (None, None)
                status_csv.writerow([wall, rid, status_of(info, now), info["reported_state"],
                                     None if info["last_heard"] is None else round(now - info["last_heard"], 2),
                                     None if info["pose_received"] is None else round(now - info["pose_received"], 2),
                                     info["backlog"], pose[0], pose[1]])
            status_file.flush()

    publish_state(mono())
    final = json.loads(args.state_json.read_text(encoding="utf-8"))
    final["survivor_reports"] = survivors
    (args.out / "final_state.json").write_text(json.dumps(final, indent=1), encoding="utf-8")
    for handle in (events_log, tasks_file, status_file):
        handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
