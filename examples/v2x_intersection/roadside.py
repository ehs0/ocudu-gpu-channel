#!/usr/bin/env python3
"""Roadside V2X server: broadcast vehicle B's state to vehicle A at 10 Hz.

A *simulated roadside sensing proxy*: B's state is the simulator's ground
truth, read from the positions feed `drive.py` publishes (the `vehicles`
object), not a detection by a real sensor.

Every `1 / --rate-hz` seconds of wall time it sends the newest B state as one
`messages.VehicleStateMessage` UDP datagram to `--dest` (vehicle A's UE
address). In the live gate it runs in the stack namespace bound to the
UPF-side address (`--bind 10.45.1.1:0`), so the datagram goes core -> gNB ->
channel emulator -> UE. `send_wall_time_ns` is taken immediately before
`sendto`. Nothing is sent while there is no fresh B state (between episodes);
the sequence number keeps counting across episodes.

Log (`--log`, CSV): one row per datagram sent.
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import signal
import socket
import time

import messages


def parse_addr(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    return host, int(port)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--positions-endpoint", required=True, help="ZMQ PUB endpoint of drive.py")
    ap.add_argument("--dest", required=True, type=parse_addr, help="vehicle A's UE address, ip:port")
    ap.add_argument("--bind", default="0.0.0.0:0", type=parse_addr)
    ap.add_argument("--vehicle", default="B")
    ap.add_argument("--rate-hz", type=float, default=10.0)
    ap.add_argument("--max-state-age-s", type=float, default=0.5,
                    help="send only while the newest B state is at most this old")
    ap.add_argument("--duration-s", type=float, default=0.0, help="0 = until SIGTERM")
    ap.add_argument("--log", type=pathlib.Path, required=True)
    args = ap.parse_args(argv)

    import zmq

    sub = zmq.Context.instance().socket(zmq.SUB)
    sub.setsockopt(zmq.LINGER, 0)
    sub.setsockopt_string(zmq.SUBSCRIBE, "")
    sub.connect(args.positions_endpoint)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(args.bind)

    stop = False

    def on_signal(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    args.log.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(args.log, "w", newline="", encoding="utf-8")
    log = csv.writer(log_file)
    log.writerow(["sequence", "send_wall_time_ns", "simulation_time", "x", "y", "vx", "vy", "bytes"])

    period = 1.0 / args.rate_hz
    latest = None            # (received monotonic, sim time, position, velocity)
    sequence = 0
    started = time.monotonic()
    next_send = started
    while not stop:
        if args.duration_s > 0 and time.monotonic() - started >= args.duration_s:
            break
        while True:
            try:
                frame = sub.recv(zmq.NOBLOCK)
            except zmq.Again:
                break
            body = json.loads(frame)
            vehicle = body.get("vehicles", {}).get(args.vehicle)
            if vehicle is not None:
                latest = (time.monotonic(), float(body["sim_time_s"]),
                          tuple(vehicle["position_m"]), tuple(vehicle["velocity_mps"]))
        now = time.monotonic()
        if now >= next_send:
            # Bound the pacing debt to one period: a stall never turns into a burst.
            next_send = max(next_send + period, now)
            if latest is not None and now - latest[0] <= args.max_state_age_s:
                _, sim_time, position, velocity = latest
                send_ns = time.time_ns()
                data = messages.encode(messages.VehicleStateMessage(
                    vehicle_id=args.vehicle, sequence=sequence, simulation_time=sim_time,
                    send_wall_time_ns=send_ns, position=position, velocity=velocity))
                udp.sendto(data, args.dest)
                log.writerow([sequence, send_ns, sim_time, position[0], position[1],
                              velocity[0], velocity[1], len(data)])
                log_file.flush()
                sequence += 1
        time.sleep(min(0.005, max(0.0, next_send - time.monotonic())))
    log_file.close()
    sub.close(linger=0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
