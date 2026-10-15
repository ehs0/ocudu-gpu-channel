#!/usr/bin/env python3
"""One rescue episode's world process: physics, sensors, positions feed and referee.

- **World**: `--world kinematic` (kinematic_world.py, development) or `isaac`
  (isaac_world.py, Isaac Sim). Physics is advanced to the wall clock
  (simulation time = elapsed wall time); it never waits for a robot, a message
  or a channel update. `result.json` records the real-time factor and late steps.
- **Body bus**: one unix datagram socket per robot. Every `1/--sensor-hz` s
  the robot gets a `sensor` frame: odometry pose, 2-D scan and the perception
  mock's detections (bearing/range, no identity). It answers with `cmd`
  (v, w, ttl); a command older than its ttl stops the robot.
- **Positions feed** (ZMQ PUB, `--positions-endpoint`): every robot's antenna as
  its UE node (`layout.UE_NODE`) for the Sionna bridge, plus robots and the
  referee's truth (survivors) for the dashboard only.
- **Referee**: reads the mission server's published state (`--central-state`),
  matches its survivor reports to the hidden survivors (within
  `--match-radius-m`) and measures the central map's coverage of the truly
  free floor. The episode succeeds when every survivor is reported and the
  coverage reaches `--coverage-target`; at `--time-limit` it fails. The
  referee is evaluation only: nothing it computes is sent to a robot or the
  mission server.

Outputs (`--out`): `result.json`, `referee.csv` (1 Hz), `poses.csv` (10 Hz), `truth.json`.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import pathlib
import signal
import socket
import sys
import time

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import grid  # noqa: E402
import layout  # noqa: E402
import messages  # noqa: E402


def make_world(name: str, survivors, robot_ids, physics_dt: float, views: bool):
    if name == "kinematic":
        from kinematic_world import KinematicWorld
        return KinematicWorld(survivors, robot_ids)
    if name == "isaac":
        from isaac_world import IsaacWorld   # the only module that imports Isaac Sim
        return IsaacWorld(survivors, robot_ids, physics_dt=physics_dt, views=views)
    raise SystemExit(f"unknown world {name!r}")


def read_central(path: pathlib.Path | None) -> dict | None:
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--run-dir", type=pathlib.Path, required=True, help="directory of the body-bus sockets")
    ap.add_argument("--world", default="kinematic", choices=("kinematic", "isaac"))
    ap.add_argument("--survivors", default="demo", help="preset name or 'random'")
    ap.add_argument("--robots", type=int, default=3, choices=(1, 2, 3), help="robot_1 .. robot_N")
    ap.add_argument("--seed", type=int, default=20261015)
    ap.add_argument("--positions-endpoint", help="ZMQ PUB endpoint for the Sionna bridge and the dashboard")
    ap.add_argument("--central-state", type=pathlib.Path, help="mission server's state JSON (referee input)")
    ap.add_argument("--time-limit", type=float, default=600.0)
    ap.add_argument("--coverage-target", type=float, default=0.80)
    ap.add_argument("--match-radius-m", type=float, default=2.0)
    ap.add_argument("--physics-hz", type=float, default=50.0)
    ap.add_argument("--sensor-hz", type=float, default=10.0)
    ap.add_argument("--pub-hz", type=float, default=10.0)
    ap.add_argument("--frames-dir", type=pathlib.Path,
                    help="write rendered views (<view>.jpg) here; Isaac world only")
    ap.add_argument("--frame-hz", type=float, default=5.0)
    ap.add_argument("--label", default="")
    ap.add_argument("--lockstep", action="store_true", help="debug only: no wall-clock pacing")
    args = ap.parse_args(argv)

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    survivors = layout.survivor_positions(args.survivors, args.seed)
    robot_ids = layout.ROBOT_IDS[:args.robots]
    world = make_world(args.world, survivors, robot_ids, 1.0 / args.physics_hz, args.frames_dir is not None)
    if args.frames_dir is not None:
        args.frames_dir.mkdir(parents=True, exist_ok=True)
    can_capture = args.frames_dir is not None and bool(getattr(world, "cameras", None))
    next_frame, frame_count, frame_ms = 0.0, 0, []
    truth_free = grid.truth_free_mask()
    (out / "truth.json").write_text(json.dumps({
        "survivors_m": [list(s) for s in survivors], "survivor_rooms": [layout.room_of(*s) for s in survivors],
        "preset": args.survivors, "seed": args.seed, "world": world.name, "robots": list(robot_ids),
        "truth_free_cells": int(truth_free.sum()), "grid": {"rows": grid.ROWS, "cols": grid.COLS,
                                                            "res_m": grid.RESOLUTION_M}}, indent=1), encoding="utf-8")

    pub = None
    if args.positions_endpoint:
        import zmq
        pub = zmq.Context.instance().socket(zmq.PUB)
        pub.setsockopt(zmq.SNDHWM, 10)
        pub.setsockopt(zmq.LINGER, 0)
        pub.bind(args.positions_endpoint)

    args.run_dir.mkdir(parents=True, exist_ok=True)
    socks, robot_addr = {}, {}
    for rid in robot_ids:
        path = args.run_dir / f"body-{rid}.sock"
        if path.exists():
            path.unlink()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.bind(str(path))
        s.setblocking(False)
        socks[rid] = s
    cmd_until = {rid: 0.0 for rid in robot_ids}
    cmd_count = {rid: 0 for rid in robot_ids}

    stop = False

    def on_signal(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    referee_file = open(out / "referee.csv", "w", newline="", encoding="utf-8")
    referee = csv.writer(referee_file)
    referee.writerow(["t_ms", "sim_time_s", "central_coverage", "reported_matched", "reported_total",
                      "central_map_revision"] + [f"{rid}_room" for rid in robot_ids])
    poses_file = open(out / "poses.csv", "w", newline="", encoding="utf-8")
    poses = csv.writer(poses_file)
    poses.writerow(["t_ms", "sim_time_s", "robot_id", "x", "y", "yaw", "v", "w", "room"])

    dt = 1.0 / args.physics_hz
    sensor_period, pub_period = 1.0 / args.sensor_hz, 1.0 / args.pub_hz
    next_sensor = next_pub = next_referee = 0.0
    late_steps, max_lag = 0, 0.0
    first_seen: dict[int, dict] = {}
    matched_at: dict[int, dict] = {}
    coverage = 0.0
    end_reason = None
    start_wall_ms = messages.now_ms()
    t0 = time.monotonic()
    steps = 0
    print(f"{start_wall_ms} event=sim_start world={world.name} survivors={survivors}", flush=True)

    def sensor_frame(rid: str, over: bool) -> dict:
        ranges, amin, ainc, rmax = world.scan(rid)
        pose = world.get_pose(rid)
        vx, vy, w = world.get_velocity(rid)
        dets = world.detections(rid)
        for det in dets:   # referee bookkeeping: which survivor did this robot see first
            ang = pose[2] + det["bearing_rad"]
            est = (pose[0] + det["range_m"] * math.cos(ang), pose[1] + det["range_m"] * math.sin(ang))
            index = min(range(len(survivors)), key=lambda i: math.hypot(survivors[i][0] - est[0],
                                                                          survivors[i][1] - est[1]))
            if index not in first_seen:
                first_seen[index] = {"robot_id": rid, "t_ms": messages.now_ms(),
                                     "sim_time_s": round(world.sim_time_s, 3)}
        return {"kind": "sensor", "t_ms": messages.now_ms(), "sim_time_s": round(world.sim_time_s, 4),
                "pose": [round(v, 4) for v in pose], "vel": [round(math.hypot(vx, vy), 3), round(w, 3)],
                "scan": {"ranges": [round(float(r), 3) for r in ranges], "angle_min": amin,
                         "angle_increment": ainc, "max_range": rmax},
                "detections": dets, "over": over}

    while not stop:
        elapsed = time.monotonic() - t0
        sim_target = elapsed if not args.lockstep else world.sim_time_s + dt
        # Commands: newest wins; stale -> stop.
        for rid, s in socks.items():
            while True:
                try:
                    data, address = s.recvfrom(65536)
                except (BlockingIOError, InterruptedError):
                    break
                if address:
                    robot_addr[rid] = address
                try:
                    body = messages.loads(data)
                except ValueError:
                    continue
                if body.get("kind") == "cmd":
                    world.set_cmd(rid, float(body["v"]), float(body["w"]))
                    cmd_until[rid] = time.monotonic() + body.get("ttl_ms", 300) / 1000.0
                    cmd_count[rid] += 1
        for rid in robot_ids:
            if time.monotonic() > cmd_until[rid]:
                world.set_cmd(rid, 0.0, 0.0)
        while world.sim_time_s + dt <= sim_target + 1e-9:
            world.step(dt)
            steps += 1
        lag = sim_target - world.sim_time_s
        max_lag = max(max_lag, lag)
        if lag > 2 * dt:
            late_steps += 1

        now = world.sim_time_s
        if now >= next_sensor:
            next_sensor = now + sensor_period
            for rid, s in socks.items():
                if rid in robot_addr:
                    try:
                        s.sendto(messages.dumps(sensor_frame(rid, end_reason is not None)), robot_addr[rid])
                    except OSError:
                        pass
        if now >= next_pub:
            next_pub = now + pub_period
            wall = messages.now_ms()
            nodes, robots = {}, {}
            for rid in robot_ids:
                x, y, yaw = world.get_pose(rid)
                vx, vy, w = world.get_velocity(rid)
                nodes[layout.UE_NODE[rid]] = ((x, y, layout.ROBOT_ANTENNA_HEIGHT_M), (vx, vy, 0.0))
                robots[rid] = {"x": round(x, 3), "y": round(y, 3), "yaw": round(yaw, 3), "node": layout.UE_NODE[rid],
                               "room": layout.room_of(x, y)}
                poses.writerow([wall, round(now, 3), rid, round(x, 3), round(y, 3), round(yaw, 4),
                                round(math.hypot(vx, vy), 3), round(w, 3), layout.room_of(x, y)])
            if pub is not None:
                pub.send_string(messages.positions_message(
                    t_unix_ms=wall, sim_time_s=now, radio_nodes=nodes, robots=robots,
                    referee={"survivors_m": [list(s) for s in survivors], "coverage": round(coverage, 4),
                             "matched": sorted(matched_at), "label": args.label}))
        if can_capture and now >= next_frame:
            next_frame = now + 1.0 / args.frame_hz
            frame_ms.append(sum(world.capture(args.frames_dir).values()))
            frame_count += 1
        if now >= next_referee:
            next_referee = now + 1.0
            central = read_central(args.central_state)
            reported = []
            map_rev = None
            if central is not None:
                reported = central.get("survivors", [])
                map_rev = central.get("map_revision")
                if central.get("map"):
                    coverage = grid.known_fraction(grid.unpack_state(central["map"]), truth_free)
                for index, (sx, sy) in enumerate(survivors):
                    if index in matched_at:
                        continue
                    for report in reported:
                        rx, ry = report["position_m"]
                        if math.hypot(rx - sx, ry - sy) <= args.match_radius_m:
                            matched_at[index] = {"t_ms": messages.now_ms(), "sim_time_s": round(now, 3),
                                                 "central_survivor_id": report["survivor_id"],
                                                 "reported_by": report.get("first_robot_id"),
                                                 "captured_at_ms": report.get("captured_at_ms"),
                                                 "received_at_ms": report.get("received_at_ms"),
                                                 "error_m": round(math.hypot(rx - sx, ry - sy), 3)}
                            break
            referee.writerow([messages.now_ms(), round(now, 3), round(coverage, 4), len(matched_at), len(reported),
                              map_rev] + [layout.room_of(*world.get_pose(rid)[:2]) for rid in robot_ids])
            referee_file.flush()
            poses_file.flush()
            if end_reason is None:
                if len(matched_at) == len(survivors) and coverage >= args.coverage_target:
                    end_reason = "success"
                elif now >= args.time_limit:
                    end_reason = "time_limit"
                if end_reason is not None:
                    end_at = time.monotonic()
                    print(f"{messages.now_ms()} event=episode_end reason={end_reason} sim_time={now:.1f}", flush=True)
        if end_reason is not None and time.monotonic() - end_at > 1.0:
            break     # robots have had a second of over=True frames

        if not args.lockstep:
            next_due = min(next_sensor, next_pub, world.sim_time_s + dt)
            sleep = (t0 + next_due) - time.monotonic()
            if sleep > 0:
                time.sleep(min(sleep, 0.01))

    wall_s = time.monotonic() - t0
    result = {
        "label": args.label, "world": world.name, "seed": args.seed, "preset": args.survivors,
        "survivors_m": [list(s) for s in survivors], "success": end_reason == "success",
        "end_reason": end_reason or "stopped", "sim_time_s": round(world.sim_time_s, 3),
        "wall_time_s": round(wall_s, 3), "start_ms": start_wall_ms,
        "timing": {"rtf": round(world.sim_time_s / wall_s, 4) if wall_s else None, "late_steps": late_steps,
                   "max_lag_ms": round(max_lag * 1000, 2), "physics_hz": args.physics_hz, "steps": steps,
                   "lockstep": args.lockstep,
                   "frames": frame_count,
                   "frame_ms_p50": round(sorted(frame_ms)[len(frame_ms) // 2], 1) if frame_ms else None},
        "coverage_final": round(coverage, 4), "coverage_target": args.coverage_target,
        "survivors": [{"index": i, "position_m": list(s), "room": layout.room_of(*s),
                       "first_seen": first_seen.get(i), "central_report": matched_at.get(i)}
                      for i, s in enumerate(survivors)],
        "robots": {rid: {"contacts": world.contacts[rid], "distance_m": round(world.distance_m[rid], 2),
                         "commands": cmd_count[rid]} for rid in robot_ids},
    }
    (out / "result.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps({"end_reason": result["end_reason"], "sim_time_s": result["sim_time_s"],
                      "coverage": result["coverage_final"], "matched": len(matched_at),
                      "rtf": result["timing"]["rtf"]}), flush=True)
    world.close()
    for rid, s in socks.items():
        s.close()
        try:
            (args.run_dir / f"body-{rid}.sock").unlink()
        except FileNotFoundError:
            pass
    referee_file.close()
    poses_file.close()
    return 0 if end_reason == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
