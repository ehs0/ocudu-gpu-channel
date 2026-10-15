#!/usr/bin/env python3
"""Run rescue episodes on one host without the radio (loopback).

The same processes as the live gate — sim.py, three robot.py, three
ue_link.py, mission_server.py — with the UE links bound to 127.0.0.1 instead of
the UEs' TUN addresses. Network modes (recorded as `network_mode` in each
episode's `run.json`; none of them is a radio result):

| `--network` | `network_mode` | What sits between UE link and server |
| --- | --- | --- |
| `ideal` | `ideal_network_baseline` | nothing |
| `blackout` | `mock_network_development` | link_proxy.py dropping one robot's traffic in fixed windows (fault injection) |
| `shadow` | `simple_loss_delay_baseline` | link_proxy.py per robot: `--delay-ms`, `--loss`, everything dropped inside `--shadow-room` |

`--bridge <scenario.json>` also runs the Sionna bridge in `--dry-run` on the
positions feed (the robots -> Sionna RT half of the loop, no broker).

    ~/venvs/rescue/bin/rescue-python examples/multi_robot_rescue/run_local.py --out results/rescue-local/<ts> \\
        --network blackout --blackout robot_2:40-60
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import layout  # noqa: E402

NETWORK_MODES = {"ideal": "ideal_network_baseline", "blackout": "mock_network_development",
                 "shadow": "simple_loss_delay_baseline"}


def git_revision() -> str:
    try:
        rev = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True,
                             check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"], capture_output=True, text=True,
                               check=True).stdout.strip()
        return rev + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def run_episode(args, episode_dir: pathlib.Path, index: int) -> int:
    episode_dir.mkdir(parents=True, exist_ok=True)
    run = pathlib.Path(tempfile.mkdtemp(prefix="rescue-"))     # short unix-socket paths (< 107 bytes)
    py = sys.executable
    mission_id = f"rescue_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{index:03d}"
    positions = f"ipc://{run}/positions.sock"
    state_json = run / "central_state.json"
    children: list[tuple[str, subprocess.Popen]] = []
    seed = args.seed + index

    def spawn(name, cmd):
        handle = open(episode_dir / f"{name}.out", "w", encoding="utf-8")
        children.append((name, subprocess.Popen(cmd, stdout=handle, stderr=subprocess.STDOUT)))

    server_port = args.port_base
    server = f"127.0.0.1:{server_port}"
    spawn("mission_server", [py, str(HERE / "mission_server.py"), "--bind", server, "--mission-id", mission_id,
                             "--out", str(episode_dir / "server"), "--state-json", str(state_json)]
          + (["--no-planner"] if args.no_planner else []))
    blackouts: dict[str, str] = {}
    for item in filter(None, args.blackout.split(";")):
        rid, _, windows = item.partition(":")
        blackouts[rid] = windows
    robot_ids = layout.ROBOT_IDS[:args.robots]
    for i, rid in enumerate(robot_ids):
        link_port = args.port_base + 10 + i
        target = server
        if args.network in ("blackout", "shadow") and (args.network == "shadow" or rid in blackouts):
            proxy_port = args.port_base + 20 + i
            target = f"127.0.0.1:{proxy_port}"
            cmd = [py, str(HERE / "link_proxy.py"), "--robot-id", rid, "--listen", target, "--server", server,
                   "--log", str(episode_dir / "proxy" / f"{rid}.csv"), "--seed", str(seed + i)]
            if args.network == "blackout":
                cmd += ["--blackout", blackouts[rid]]
            else:
                cmd += ["--delay-ms", str(args.delay_ms), "--loss", str(args.loss), "--shadow-room", args.shadow_room,
                        "--positions-endpoint", positions]
            spawn(f"proxy-{rid}", cmd)
        spawn(f"ue_link-{rid}", [py, str(HERE / "ue_link.py"), "--robot-id", rid, "--bind", f"127.0.0.1:{link_port}",
                                 "--server", target, "--bus-socket", str(run / f"link-{rid}.sock"),
                                 "--queue-db", str(run / f"queue-{rid}.db"), "--log-dir", str(episode_dir / "links" / rid),
                                 "--status-json", str(run / f"link-{rid}.json"),
                                 "--ready-file", str(run / f"link-{rid}.ready")])
    if args.bridge is not None:
        spawn("bridge", [args.bridge_python, str(REPO / "scripts" / "sionna_rt" / "run_bridge.py"),
                         "--scenario-config", str(args.bridge.resolve()), "--dry-run", "--duration", "0",
                         "--update-hz", str(args.bridge_update_hz), "--position-endpoint", positions,
                         "--status-jsonl", str(episode_dir / "bridge-status.jsonl")])
    ready = [run / f"link-{rid}.ready" for rid in robot_ids]
    deadline = time.monotonic() + 60.0
    while not all(path.exists() for path in ready):
        if time.monotonic() > deadline or any(child.poll() is not None for _, child in children):
            for _, child in children:
                child.kill()
            raise SystemExit(f"helpers did not start; see {episode_dir}/*.out")
        time.sleep(0.1)

    meta = {"mission_id": mission_id, "network": args.network, "network_mode": NETWORK_MODES[args.network],
            "radio": "none (loopback)", "world": args.world, "robots": args.robots, "seed": seed, "survivors": args.survivors,
            "time_limit_s": args.time_limit, "coverage_target": args.coverage_target,
            "blackout": blackouts or None, "shadow_room": args.shadow_room if args.network == "shadow" else None,
            "delay_ms": args.delay_ms if args.network == "shadow" else None,
            "loss": args.loss if args.network == "shadow" else None, "planner": not args.no_planner,
            "central_tasks_followed": not args.no_central, "git": git_revision(),
            "python": sys.version.split()[0], "started_ms": time.time_ns() // 1_000_000,
            "run_dir": str(run), "positions_endpoint": positions}
    (episode_dir / "run.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    if args.web_status_dir is not None:
        args.web_status_dir.mkdir(parents=True, exist_ok=True)
        (args.web_status_dir / "current.json").write_text(json.dumps({"run_dir": str(run),
                                                                      "episode_dir": str(episode_dir),
                                                                      **meta}), encoding="utf-8")
    sim_cmd = [py, str(HERE / "sim.py"), "--out", str(episode_dir / "sim"), "--run-dir", str(run),
               "--world", args.world, "--survivors", args.survivors, "--seed", str(seed), "--robots", str(args.robots),
               "--positions-endpoint", positions, "--central-state", str(state_json),
               "--time-limit", str(args.time_limit), "--coverage-target", str(args.coverage_target),
               "--label", NETWORK_MODES[args.network], "--frames-dir", str(run / "frames")]
    sim_handle = open(episode_dir / "sim.out", "w", encoding="utf-8")
    sim = subprocess.Popen(sim_cmd, stdout=sim_handle, stderr=subprocess.STDOUT)
    time.sleep(0.5)
    for rid in robot_ids:
        spawn(f"robot-{rid}", [py, str(HERE / "robot.py"), "--robot-id", rid, "--mission-id", mission_id,
                               "--body-socket", str(run / f"body-{rid}.sock"),
                               "--own-socket", str(run / f"robot-{rid}.sock"),
                               "--link-socket", str(run / f"link-{rid}.sock"),
                               "--log-dir", str(episode_dir / "robots"),
                               "--status-json", str(run / f"robot-{rid}.json"),
                               "--max-seconds", str(args.time_limit + 30)]
              + (["--no-central"] if args.no_central else []))
    status = sim.wait()
    time.sleep(args.drain_s)      # let the links deliver what is still queued
    for name, child in reversed(children):
        child.send_signal(signal.SIGTERM)
    for name, child in children:
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
    for path in run.glob("*.db"):
        shutil.copy(path, episode_dir / "links" / path.name)
    shutil.rmtree(run, ignore_errors=True)
    subprocess.run([py, str(HERE / "analyze.py"), str(episode_dir)], check=False)
    return status


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--episodes", type=int, default=1)
    ap.add_argument("--network", default="ideal", choices=sorted(NETWORK_MODES))
    ap.add_argument("--blackout", default="robot_2:40-60",
                    help="--network blackout: 'robot_x:a-b,c-d;robot_y:...' seconds after the link starts")
    ap.add_argument("--shadow-room", default=layout.SHADOW_ROOM)
    ap.add_argument("--delay-ms", type=float, default=0.0)
    ap.add_argument("--loss", type=float, default=0.0)
    ap.add_argument("--world", default="kinematic", choices=("kinematic", "isaac"))
    ap.add_argument("--survivors", default="demo")
    ap.add_argument("--robots", type=int, default=3, choices=(1, 2, 3))
    ap.add_argument("--seed", type=int, default=20261015)
    ap.add_argument("--time-limit", type=float, default=600.0)
    ap.add_argument("--coverage-target", type=float, default=0.80)
    ap.add_argument("--no-planner", action="store_true", help="mission server sends no tasks")
    ap.add_argument("--no-central", action="store_true", help="robots ignore central tasks")
    ap.add_argument("--drain-s", type=float, default=3.0)
    ap.add_argument("--port-base", type=int, default=7200)
    ap.add_argument("--bridge", type=pathlib.Path, help="Sionna scenario JSON: also run the bridge dry-run")
    ap.add_argument("--bridge-python", default=os.environ.get("OCUDU_NATIVE_SIONNA_PYTHON", "python3"))
    ap.add_argument("--bridge-update-hz", type=float, default=10.0)
    ap.add_argument("--web-status-dir", type=pathlib.Path, help="write current.json here for web/server.py")
    args = ap.parse_args(argv)

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    status = 0
    for index in range(args.episodes):
        episode = out / "episodes" / f"e{index:03d}-{args.network}"
        status |= run_episode(args, episode, index)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
