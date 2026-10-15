#!/usr/bin/env python3
"""One robot's local autonomy: mapping, frontier exploration, survivor confirmation (numpy).

Inputs are the robot's own: odometry, 2-D scan and perception-mock detections
from the body bus (sim.py), and whatever its UE link (ue_link.py) delivers —
mission tasks and the link state. Outputs: body velocity commands, and events
(map tiles, pose, survivor reports, task ACKs) handed to the UE link. The
process opens no network socket, only unix datagram sockets, so it has no path
to the mission server except through its UE link.

Behaviour (MULTI_ROBOT_RESCUE_MILESTONES.md, shadow-region policy):

- A valid central task (`valid_until_ms` in the future, newer revision, target
  reachable in the robot's own map with unknown space allowed) is followed
  while it lasts; otherwise the robot picks its own frontier.
- The link state is logged and reported but never changes how the robot
  moves: an OFFLINE robot keeps exploring, never turns back for coverage and
  never waits.
- Events are generated whatever the link state; the UE link stores them.
- The robot stops only on episode end, a lost sensor stream (fail-safe), or
  when no reachable frontier and no task remain (`IDLE`).
"""

from __future__ import annotations

import argparse
import collections
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

CONFIRM_HITS = 3
ASSOCIATE_M = 1.0
GOAL_REACHED_M = 0.5
STUCK_WINDOW_S = 8.0           # no STUCK_PROGRESS_M closer to the same goal for this long -> stuck
STUCK_PROGRESS_M = 0.3
BLACKLIST_S = 30.0
SENSOR_TIMEOUT_S = 0.5
REPLAN_S = 1.0
TILE_PERIOD_S = 2.0
POSE_PERIOD_S = 1.0
FRONT_CLEARANCE_M = 0.12        # free distance required ahead of the robot's front edge
SIDE_MARGIN_M = 0.0             # the planner already keeps clear of obstacles (grid.HARD_INFLATE_CELLS)
MAX_V_MPS = 0.6
BLOCKED_RECOVERY_S = 1.5
BACKUP_S = 1.2
BACKUP_V_MPS = 0.2
CMD_TTL_MS = 300


class Planner:
    """Goal choice and path following on the robot's own grid."""

    def __init__(self):
        self.goal: tuple[int, int] | None = None
        self.goal_source = "none"
        self.dist: np.ndarray | None = None
        self.blacklist: list[tuple[float, float, float]] = []   # (x, y, until_mono)

    def blacklisted(self, xy, now) -> bool:
        self.blacklist = [b for b in self.blacklist if b[2] > now]
        return any(math.hypot(xy[0] - bx, xy[1] - by) < 1.0 for bx, by, _ in self.blacklist)

    def plan_to(self, state, robot_cell, goal_cell, *, allow_unknown: bool, snap_cells: int = 0) -> bool:
        passable = grid.traversable(state, allow_unknown=allow_unknown)
        r, c = robot_cell
        passable[max(r - 1, 0):r + 2, max(c - 1, 0):c + 2] = True
        if snap_cells and not passable[goal_cell]:
            # A central target may sit in this robot's inflated band: take the nearest passable cell.
            gr, gc = goal_cell
            near = [(dr * dr + dc * dc, gr + dr, gc + dc)
                    for dr in range(-snap_cells, snap_cells + 1) for dc in range(-snap_cells, snap_cells + 1)
                    if grid.in_grid(gr + dr, gc + dc) and passable[gr + dr, gc + dc]]
            if not near:
                return False
            goal_cell = min(near)[1:]
        dist = grid.distance_field(passable, [goal_cell], grid.step_cost(state))
        if not np.isfinite(dist[robot_cell]):
            return False
        self.dist, self.goal = dist, goal_cell
        return True

    def choose_frontier(self, state, robot_cell, now) -> tuple[int, int] | None:
        passable = grid.traversable(state, allow_unknown=False)
        r, c = robot_cell
        passable[max(r - 1, 0):r + 2, max(c - 1, 0):c + 2] = True
        dist = grid.distance_field(passable, [robot_cell], grid.step_cost(state))
        best = None
        for cluster in grid.frontier_clusters(state):
            found = grid.cluster_goal(cluster, dist)
            if found is None:
                continue
            cell, d = found
            if self.blacklisted(grid.centre_of(*cell), now):
                continue
            cost = d * grid.RESOLUTION_M - 0.3 * len(cluster) * grid.RESOLUTION_M
            if best is None or cost < best[0]:
                best = (cost, cell)
        return None if best is None else best[1]

    def control(self, pose, robot_cell) -> tuple[float, float]:
        if self.dist is None:
            return 0.0, 0.0
        path = grid.descend(self.dist, robot_cell, 6)
        target = grid.centre_of(*path[min(len(path) - 1, 4)])
        x, y, yaw = pose
        err = layout.wrap_angle(math.atan2(target[1] - y, target[0] - x) - yaw)
        w = max(-1.5, min(1.5, 2.0 * err))
        v = MAX_V_MPS * max(0.0, math.cos(err)) ** 2 if abs(err) < 0.9 else 0.0
        return v, w


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--robot-id", required=True, choices=layout.ROBOT_IDS)
    ap.add_argument("--mission-id", required=True)
    ap.add_argument("--body-socket", type=pathlib.Path, required=True, help="sim.py's unix socket for this robot")
    ap.add_argument("--own-socket", type=pathlib.Path, required=True)
    ap.add_argument("--link-socket", type=pathlib.Path, required=True, help="ue_link.py's bus socket")
    ap.add_argument("--log-dir", type=pathlib.Path, required=True)
    ap.add_argument("--status-json", type=pathlib.Path)
    ap.add_argument("--no-central", action="store_true", help="ignore mission tasks (local-only ablation)")
    ap.add_argument("--max-seconds", type=float, default=900.0)
    args = ap.parse_args(argv)

    rid = args.robot_id
    boot = f"R{messages.now_ms()}"
    seq = 0
    mono = time.monotonic
    args.log_dir.mkdir(parents=True, exist_ok=True)
    log = open(args.log_dir / f"{rid}.jsonl", "w", encoding="utf-8")

    def note(event: str, **fields) -> None:
        log.write(json.dumps({"t_ms": messages.now_ms(), "robot_id": rid, "event": event, **fields}) + "\n")
        log.flush()

    if args.own_socket.exists():
        args.own_socket.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(args.own_socket))
    sock.setblocking(False)

    outbox: collections.deque = collections.deque(maxlen=20000)
    counters = collections.Counter()

    def to_link(body: dict) -> bool:
        try:
            sock.sendto(messages.dumps(body), str(args.link_socket))
            return True
        except OSError:
            return False

    def emit(type_: str, payload: dict, pose, confidence=None, captured_ms=None) -> dict:
        nonlocal seq
        seq += 1
        event = messages.make_event(mission_id=args.mission_id, robot_id=rid, boot=boot, seq=seq, type_=type_,
                                    captured_at_ms=captured_ms or messages.now_ms(), pose=pose,
                                    payload=payload, confidence=confidence)
        outbox.append(event)
        counters[f"emitted_{type_}"] += 1
        return event

    def flush_outbox() -> None:
        while outbox:
            if not to_link({"kind": "event", "event": outbox[0]}):
                counters["link_bus_unavailable"] += 1
                return
            outbox.popleft()

    def to_body(body: dict) -> None:
        try:
            sock.sendto(messages.dumps(body), str(args.body_socket))
        except OSError:
            counters["body_bus_unavailable"] += 1

    stop = False

    def on_signal(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    occ = grid.OccupancyGrid()
    planner = Planner()
    link_state, link_backlog = "UNKNOWN", None
    task: dict | None = None
    last_revision = 0
    candidates: list[dict] = []
    mode = "BOOT"
    pose = layout.SPAWN_POSES[rid]
    last_sensor = None
    last_replan = last_tiles = last_pose_event = 0.0
    progress = {"goal": None, "best": math.inf, "since": mono()}    # closest approach to the current goal
    cmd_seq = 0
    blocked_since: float | None = None
    backup_until = 0.0
    sim_time = 0.0
    over = False
    start = mono()
    last_hello = -math.inf
    note("start", mission_id=args.mission_id, boot=boot, central=not args.no_central)

    def set_mode(new: str, **fields) -> None:
        nonlocal mode
        if new != mode:
            note("mode", previous=mode, mode=new, **fields)
            mode = new

    while not stop and not over and mono() - start < args.max_seconds:
        try:
            ready, _, _ = select.select([sock], [], [], 0.02)
        except InterruptedError:
            continue
        now = mono()
        if last_sensor is None and now - last_hello >= 1.0:
            # Until the world answers: it learns this robot's address from a datagram, and it may
            # bind its socket after this process starts (Isaac Sim takes seconds to come up).
            last_hello = now
            to_link({"kind": "hello", "robot_id": rid, "reply_to": str(args.own_socket)})
            to_body({"kind": "hello", "robot_id": rid})
        sensor = None
        while ready:
            try:
                data, _ = sock.recvfrom(1 << 20)
            except (BlockingIOError, InterruptedError):
                break
            try:
                body = messages.loads(data)
            except ValueError:
                counters["bus_invalid"] += 1
                continue
            kind = body.get("kind")
            if kind == "sensor":
                sensor = body              # newest wins
            elif kind == "link_state":
                if body.get("state") != link_state:
                    note("link_state", state=body.get("state"), backlog=body.get("backlog"))
                link_state, link_backlog = body.get("state"), body.get("backlog")
            elif kind == "task":
                counters["tasks_rx"] += 1
                raw = body.get("task")
                if task is not None and isinstance(raw, dict) and raw.get("command_id") == task["command_id"]:
                    counters["tasks_duplicate"] += 1    # the server resent a task we already hold
                    continue
                try:
                    candidate = messages.validate_task(body.get("task"))
                    reason = messages.check_task(candidate, robot_id=rid, mission_id=args.mission_id,
                                                 last_revision=last_revision, now=messages.now_ms())
                except ValueError as exc:
                    candidate, reason = body.get("task") or {}, f"invalid: {exc}"
                if args.no_central and reason is None:
                    reason = "central_disabled"
                if reason is None and planner.blacklisted((candidate["target_pose_m"]["x"],
                                                           candidate["target_pose_m"]["y"]), mono()):
                    reason = "blacklisted"     # this robot got stuck going there recently
                command_id = str(candidate.get("command_id", "?"))
                if reason is None:
                    task, last_revision = candidate, candidate["revision"]
                    planner.goal = None
                    last_replan = 0.0
                note("task", command_id=command_id, revision=candidate.get("revision"),
                     accepted=reason is None, reason=reason, target=candidate.get("target_pose_m"),
                     valid_until_ms=candidate.get("valid_until_ms"))
                emit("mission_task_ack", {"command_id": command_id, "accepted": reason is None,
                                          "reason": reason or "", "revision": candidate.get("revision")}, pose)

        if sensor is not None:
            last_sensor = now
            sim_time = float(sensor["sim_time_s"])
            over = bool(sensor.get("over"))
            pose = tuple(sensor["pose"])
            scan = sensor["scan"]
            occ.integrate_scan(pose, np.asarray(scan["ranges"], dtype=np.float32), scan["angle_min"],
                               scan["angle_increment"], scan["max_range"])
            counters["scans"] += 1
            # Survivor candidates from the perception mock (bearing/range only).
            for det in sensor.get("detections", []):
                ang = pose[2] + det["bearing_rad"]
                est = (pose[0] + det["range_m"] * math.cos(ang), pose[1] + det["range_m"] * math.sin(ang))
                match = next((c for c in candidates if math.hypot(c["x"] - est[0], c["y"] - est[1]) < ASSOCIATE_M),
                             None)
                if match is None:
                    match = {"candidate_id": f"{rid}-c{len(candidates) + 1}", "x": est[0], "y": est[1], "hits": 0,
                             "confidence": 0.0, "first_seen_ms": messages.now_ms(), "confirmed": False}
                    candidates.append(match)
                match["hits"] += 1
                k = match["hits"]
                match["x"] += (est[0] - match["x"]) / k
                match["y"] += (est[1] - match["y"]) / k
                match["confidence"] = max(match["confidence"], det["confidence"])
                if not match["confirmed"] and k >= CONFIRM_HITS:
                    match["confirmed"] = True
                    wall = messages.now_ms()
                    event = emit("survivor_detected",
                                 {"candidate_id": match["candidate_id"],
                                  "position_m": [round(match["x"], 2), round(match["y"], 2)],
                                  "hits": k, "evidence": "perception_mock"},
                                 pose, confidence=match["confidence"], captured_ms=wall)
                    note("survivor_confirmed", candidate_id=match["candidate_id"], event_id=event["event_id"],
                         position_m=[round(match["x"], 2), round(match["y"], 2)], sim_time_s=sim_time,
                         link_state=link_state)

        # Fail-safe: no sensor stream -> stop (robotics safety, not a reaction to the radio).
        if last_sensor is None or now - last_sensor > SENSOR_TIMEOUT_S:
            if last_sensor is not None:
                set_mode("SAFETY_STOP", reason="sensor_timeout")
            flush_outbox()
            continue

        cell = grid.cell_of(pose[0], pose[1])
        state = occ.state()
        task_valid = task is not None and messages.now_ms() < task["valid_until_ms"]
        if task is not None and not task_valid:
            note("task_expired", command_id=task["command_id"])
            task = None
            planner.goal = None

        if planner.goal is not None:
            gx, gy = grid.centre_of(*planner.goal)
            if math.hypot(gx - pose[0], gy - pose[1]) < GOAL_REACHED_M:
                if planner.goal_source == "central" and task is not None:
                    note("task_reached", command_id=task["command_id"])
                    task = None
                # Been there: neither the local frontier choice nor a renewed central task sends it back soon.
                planner.blacklist.append((gx, gy, now + BLACKLIST_S))
                planner.goal = None
        if planner.goal is not None:
            # Stuck = no progress toward (about) the same goal, whatever the robot does meanwhile;
            # a renewed task for the same target keeps the clock running.
            gxy = grid.centre_of(*planner.goal)
            d = math.hypot(gxy[0] - pose[0], gxy[1] - pose[1])
            same = progress["goal"] is not None and math.hypot(gxy[0] - progress["goal"][0],
                                                               gxy[1] - progress["goal"][1]) < 0.6
            if not same or d < progress["best"] - STUCK_PROGRESS_M:
                progress.update(goal=gxy, best=d if not same else min(d, progress["best"]), since=now)
            elif now - progress["since"] > STUCK_WINDOW_S:
                planner.blacklist.append((gxy[0], gxy[1], now + BLACKLIST_S))
                note("stuck", goal=[round(gxy[0], 2), round(gxy[1], 2)], source=planner.goal_source)
                if planner.goal_source == "central" and task is not None:
                    emit("mission_task_ack", {"command_id": task["command_id"], "accepted": False,
                                              "reason": "stuck", "revision": task["revision"]}, pose)
                    task = None
                planner.goal = None
                progress.update(goal=None, best=math.inf, since=now)

        if planner.goal is None or now - last_replan >= REPLAN_S:
            last_replan = now
            planned = False
            if task is not None:
                target = grid.cell_of(task["target_pose_m"]["x"], task["target_pose_m"]["y"])
                if grid.in_grid(*target) and planner.plan_to(state, cell, target, allow_unknown=True, snap_cells=5):
                    planner.goal_source, planned = "central", True
                else:
                    note("task_unreachable", command_id=task["command_id"])
                    emit("mission_task_ack", {"command_id": task["command_id"], "accepted": False,
                                              "reason": "unreachable", "revision": task["revision"]}, pose)
                    task = None
            if not planned:
                keep = (planner.goal is not None and planner.goal_source == "local"
                        and state[planner.goal] == grid.FREE and grid.frontier_mask(state)[planner.goal])
                goal = planner.goal if keep else planner.choose_frontier(state, cell, now)
                if goal is not None and planner.plan_to(state, cell, goal, allow_unknown=False):
                    if not keep:
                        gxy = grid.centre_of(*goal)
                        note("goal", source="local", goal=[round(gxy[0], 2), round(gxy[1], 2)], link_state=link_state)
                    planner.goal_source = "local"
                else:
                    planner.goal, planner.dist, planner.goal_source = None, None, "none"
            set_mode({"central": "CENTRAL_TASK", "local": "LOCAL_FRONTIER", "none": "IDLE"}[planner.goal_source])

        v, w = planner.control(pose, cell) if planner.goal is not None else (0.0, 0.0)
        # Reactive safety layer on the raw scan: is the robot-wide lane ahead clear for
        # FRONT_CLEARANCE_M beyond the front edge? If not, stop and turn toward the open side;
        # blocked for longer than BLOCKED_RECOVERY_S -> back up for BACKUP_S if the rear lane is clear.
        if sensor is not None:
            ranges = np.asarray(sensor["scan"]["ranges"])
            angles = sensor["scan"]["angle_min"] + sensor["scan"]["angle_increment"] * np.arange(len(ranges))
            along = np.cos(angles) * ranges
            lateral = np.abs(np.sin(angles) * ranges)
            lane = lateral < layout.ROBOT_RADIUS_M + SIDE_MARGIN_M
            front, rear = lane & (along > 0), lane & (along < 0)
            limit = layout.ROBOT_RADIUS_M + FRONT_CLEARANCE_M
            front_blocked = front.any() and along[front].min() < limit
            rear_clear = not rear.any() or (-along[rear]).min() >= limit
            if now < backup_until and rear_clear:
                v, w = -BACKUP_V_MPS, 0.0
            elif v > 0 and front_blocked:
                if blocked_since is None:
                    blocked_since = now
                left = ranges[(angles > 0) & (angles < math.radians(100))].mean()
                right = ranges[(angles < 0) & (angles > -math.radians(100))].mean()
                v = 0.0          # keep the planner's turn; add one only when it would not turn
                if abs(w) < 0.3:
                    w = 1.0 if left > right else -1.0
                counters["safety_stops"] += 1
                if now - blocked_since > BLOCKED_RECOVERY_S and rear_clear:
                    backup_until, blocked_since = now + BACKUP_S, None
                    counters["backups"] += 1
            else:
                blocked_since = None
        if sensor is not None:
            cmd_seq += 1
            to_body({"kind": "cmd", "robot_id": rid, "seq": cmd_seq, "v": round(v, 3), "w": round(w, 3),
                     "ttl_ms": CMD_TTL_MS})

        if now - last_tiles >= TILE_PERIOD_S:
            last_tiles = now
            for tx, ty in occ.changed_tiles():
                emit("map_tile", grid.tile_payload(occ.sent_state, tx, ty), pose)
        if now - last_pose_event >= POSE_PERIOD_S:
            last_pose_event = now
            goal_xy = None if planner.goal is None else [round(v_, 2) for v_ in grid.centre_of(*planner.goal)]
            emit("robot_pose", {"mode": mode, "goal_m": goal_xy, "goal_source": planner.goal_source,
                                "sim_time_s": round(sim_time, 2), "candidates": len(candidates)}, pose)
            if args.status_json is not None:
                status = {"t_ms": messages.now_ms(), "robot_id": rid, "mode": mode, "link_state": link_state,
                          "link_backlog": link_backlog, "pose": [round(p, 3) for p in pose], "goal_m": goal_xy,
                          "goal_source": planner.goal_source,
                          "task": None if task is None else {k: task[k] for k in ("command_id", "revision",
                                                                                  "valid_until_ms", "target_pose_m")},
                          "candidates": [{k: (round(c[k], 2) if isinstance(c[k], float) else c[k])
                                          for k in ("candidate_id", "x", "y", "hits", "confirmed")}
                                         for c in candidates],
                          "outbox": len(outbox), "counters": dict(counters),
                          "known_cells": int((state != grid.UNKNOWN).sum())}
                tmp = args.status_json.with_suffix(".tmp")
                tmp.write_text(json.dumps(status, separators=(",", ":")), encoding="utf-8")
                os.replace(tmp, args.status_json)
        flush_outbox()

    to_body({"kind": "cmd", "robot_id": rid, "seq": cmd_seq + 1, "v": 0.0, "w": 0.0, "ttl_ms": CMD_TTL_MS})
    flush_outbox()
    state = occ.state()
    result = {"robot_id": rid, "mission_id": args.mission_id, "boot": boot, "events_emitted": seq,
              "outbox_left": len(outbox), "counters": dict(counters), "final_mode": mode,
              "candidates": candidates, "known_cells": int((state != grid.UNKNOWN).sum()),
              "local_map": grid.pack_state(state), "sim_time_s": sim_time}
    (args.log_dir / f"{rid}-result.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    note("end", over=over, events_emitted=seq, outbox_left=len(outbox))
    log.close()
    try:
        args.own_socket.unlink()
    except FileNotFoundError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
