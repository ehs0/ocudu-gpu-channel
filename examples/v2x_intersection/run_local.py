#!/usr/bin/env python3
"""Run episodes on one host without the radio: the V2X datagrams go over loopback.

Same processes as the live gate — roadside.py, ue_client.py (bound to
127.0.0.1 instead of a UE's TUN address) and one drive.py per episode — so the
control loop and the logs are identical, but there is no 5G link. This is the
no-radio reference, not an integrated result.

`--bridge <scenario.json>` also starts the Sionna bridge in `--dry-run` mode
(traces the channel and writes its status JSONL, no broker) subscribed to
the episodes' positions feed: the SUMO -> Sionna RT half of the loop without
the radio stack.

    ~/venvs/v2x/bin/v2x-python examples/v2x_intersection/run_local.py \
        --out results/v2x-local/<ts> --modes v2x,no-v2x --episodes 2
"""

from __future__ import annotations

import argparse
import os
import pathlib
import signal
import shutil
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--modes", default="v2x,no-v2x",
                    help="comma list cycled per episode: v2x | no-v2x (controller ignores V2X), "
                         "optionally :cautious | :none to set the fallback")
    ap.add_argument("--episodes", type=int, default=1, help="episodes per mode")
    ap.add_argument("--port", type=int, default=7100)
    ap.add_argument("--bridge", type=pathlib.Path, help="Sionna scenario JSON: also run the bridge dry-run")
    ap.add_argument("--bridge-python", default=os.environ.get("OCUDU_NATIVE_SIONNA_PYTHON", "python3"))
    ap.add_argument("--bridge-update-hz", type=float, default=10.0)
    ap.add_argument("--drive-args", default="", help="extra words for every drive.py")
    args = ap.parse_args(argv)

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    # Unix and ipc:// socket paths must stay under 107 bytes; keep them short.
    run = pathlib.Path(tempfile.mkdtemp(prefix="v2x-"))
    positions = f"ipc://{run}/positions.sock"
    relay = run / "v2x-relay.sock"
    py = sys.executable
    children: list[subprocess.Popen] = []

    def spawn(cmd, log_name):
        handle = open(out / log_name, "w", encoding="utf-8")
        child = subprocess.Popen(cmd, stdout=handle, stderr=subprocess.STDOUT)
        children.append(child)
        return child

    spawn([py, str(HERE / "ue_client.py"), "--bind", f"127.0.0.1:{args.port}",
           "--relay-socket", str(relay), "--log", str(out / "ue_rx.csv"),
           "--ready-file", str(run / "ue-ready")], "ue_client.out")
    spawn([py, str(HERE / "roadside.py"), "--positions-endpoint", positions,
           "--dest", f"127.0.0.1:{args.port}", "--log", str(out / "roadside_tx.csv")], "roadside.out")
    if args.bridge is not None:
        spawn([args.bridge_python, str(REPO / "scripts" / "sionna_rt" / "run_bridge.py"),
               "--scenario-config", str(args.bridge.resolve()), "--dry-run", "--duration", "0",
               "--update-hz", str(args.bridge_update_hz), "--position-endpoint", positions,
               "--status-jsonl", str(out / "bridge-status.jsonl")], "bridge.out")
    ready = [run / "ue-ready"] + ([out / "bridge-status.jsonl"] if args.bridge is not None else [])
    deadline = time.monotonic() + 120.0
    while not all(path.exists() for path in ready):
        if time.monotonic() > deadline or any(child.poll() is not None for child in children):
            for child in children:
                child.kill()
            raise SystemExit(f"helpers did not start (waited for {', '.join(map(str, ready))})")
        time.sleep(0.1)

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    status = 0
    try:
        for index in range(args.episodes * len(modes)):
            mode = modes[index % len(modes)]
            use, _, fallback = mode.partition(":")
            episode = out / "episodes" / f"e{index:03d}-{mode.replace(':', '-')}"
            cmd = [py, str(HERE / "drive.py"), "--out", str(episode), "--v2x-socket", str(relay),
                   "--positions-endpoint", positions, "--label", mode] + args.drive_args.split()
            if use not in ("v2x", "no-v2x"):
                raise SystemExit(f"unknown mode {mode!r}")
            if use == "no-v2x":
                cmd.append("--no-v2x")
            if fallback:
                cmd += ["--fallback", fallback]
            status |= subprocess.run(cmd, check=False).returncode
            time.sleep(1.0)
    finally:
        for child in children:
            child.send_signal(signal.SIGTERM)
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        shutil.rmtree(run, ignore_errors=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
