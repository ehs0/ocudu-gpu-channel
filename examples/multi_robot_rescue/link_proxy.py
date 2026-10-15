#!/usr/bin/env python3
"""UDP impairment proxy between one robot's UE link and the mission server (loopback runs only).

Not a radio model and never part of the live gate. It provides the two
no-radio references of run_local.py:

- **fault injection** (`--blackout 40-60,...`, seconds after start): every
  datagram in both directions is dropped inside the windows. Used to test the
  offline behaviour (`mock_network_development`).
- **simple loss/delay baseline** (`--delay-ms`, `--loss`, and `--shadow-room`
  with `--positions-endpoint`: drop everything while this robot's position is
  in that room). The room rule is the "shadow polygon => offline" model the
  live gate replaces with the emulated channel (`simple_loss_delay_baseline`).

Log (`--log`, CSV): one row per datagram: time, direction, size, fate.
"""

from __future__ import annotations

import argparse
import csv
import heapq
import json
import pathlib
import random
import select
import signal
import socket
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import layout  # noqa: E402


def parse_windows(text: str) -> list[tuple[float, float]]:
    windows = []
    for part in filter(None, (p.strip() for p in text.split(","))):
        a, _, b = part.partition("-")
        windows.append((float(a), float(b)))
    return windows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--robot-id", required=True)
    ap.add_argument("--listen", required=True, help="ip:port the UE link sends to")
    ap.add_argument("--server", required=True, help="mission server ip:port")
    ap.add_argument("--delay-ms", type=float, default=0.0, help="one-way, each direction")
    ap.add_argument("--loss", type=float, default=0.0, help="drop probability per datagram, each direction")
    ap.add_argument("--blackout", default="", help="windows a-b in seconds after start, comma separated")
    ap.add_argument("--shadow-room", default="", help="drop all while the robot is in this layout room")
    ap.add_argument("--positions-endpoint", help="positions feed (needed with --shadow-room)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--log", type=pathlib.Path, required=True)
    args = ap.parse_args(argv)

    rng = random.Random(args.seed)
    windows = parse_windows(args.blackout)
    lhost, _, lport = args.listen.rpartition(":")
    shost, _, sport = args.server.rpartition(":")
    server = (shost, int(sport))
    front = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    front.bind((lhost, int(lport)))
    front.setblocking(False)
    back = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    back.bind((lhost, 0))
    back.setblocking(False)
    sub = None
    if args.shadow_room:
        if not args.positions_endpoint:
            raise SystemExit("--shadow-room needs --positions-endpoint")
        import zmq
        sub = zmq.Context.instance().socket(zmq.SUB)
        sub.setsockopt_string(zmq.SUBSCRIBE, "")
        sub.connect(args.positions_endpoint)
    in_shadow = False
    client = None
    pending: list[tuple[float, int, bytes, str]] = []
    counter = 0
    args.log.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(args.log, "w", newline="", encoding="utf-8")
    log = csv.writer(log_file)
    log.writerow(["t_ms", "direction", "bytes", "fate"])
    t0 = time.monotonic()
    stop = False

    def on_signal(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    print(f"{time.time_ns() // 1_000_000} event=proxy_ready robot={args.robot_id} listen={args.listen} "
          f"blackout={windows} shadow_room={args.shadow_room or '-'}", flush=True)

    while not stop:
        now = time.monotonic()
        timeout = 0.01 if pending else 0.05
        try:
            ready, _, _ = select.select([front, back], [], [], timeout)
        except InterruptedError:
            continue
        if sub is not None:
            while sub.poll(0):
                frame = json.loads(sub.recv())
                robot = frame.get("robots", {}).get(args.robot_id)
                if robot is not None:
                    in_shadow = layout.room_of(robot["x"], robot["y"]) == args.shadow_room
        now = time.monotonic()
        elapsed = now - t0
        blocked = in_shadow or any(a <= elapsed < b for a, b in windows)
        for sock, direction in ((front, "up"), (back, "down")):
            if sock not in ready:
                continue
            while True:
                try:
                    data, addr = sock.recvfrom(65536)
                except (BlockingIOError, InterruptedError):
                    break
                if direction == "up":
                    client = addr
                if blocked:
                    fate = "blackout" if not in_shadow else "shadow"
                elif rng.random() < args.loss:
                    fate = "loss"
                else:
                    fate = "forwarded"
                    counter += 1
                    heapq.heappush(pending, (now + args.delay_ms / 1000.0, counter, data, direction))
                log.writerow([time.time_ns() // 1_000_000, direction, len(data), fate])
        while pending and pending[0][0] <= time.monotonic():
            _, _, data, direction = heapq.heappop(pending)
            try:
                if direction == "up":
                    back.sendto(data, server)
                elif client is not None:
                    front.sendto(data, client)
            except OSError:
                pass
        log_file.flush()
    log_file.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
