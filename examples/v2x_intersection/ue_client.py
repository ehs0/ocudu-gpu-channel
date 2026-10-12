#!/usr/bin/env python3
"""V2X client on vehicle A's UE (stdlib only).

Runs inside the UE's network namespace in the live gate (bound to the
UE's TUN address), so every datagram it receives came over the 5G user plane
through the channel emulator. It stamps the receive time, logs the datagram
and relays it to the vehicle controller (`drive.py --v2x-socket`) over a
filesystem unix datagram socket — the in-vehicle bus; unix sockets are not
tied to a network namespace. While no episode is running the relay socket does
not exist; those datagrams are logged with `relayed=0`.

Log (`--log`, CSV): one row per datagram received.
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import signal
import socket
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import messages  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bind", required=True, help="ip:port (the UE's TUN address in the live gate)")
    ap.add_argument("--relay-socket", type=pathlib.Path, required=True)
    ap.add_argument("--log", type=pathlib.Path, required=True)
    ap.add_argument("--ready-file", type=pathlib.Path, help="touched once the socket is bound")
    args = ap.parse_args(argv)

    host, _, port = args.bind.rpartition(":")
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind((host, int(port)))
    udp.settimeout(0.5)
    relay = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

    stop = False

    def on_signal(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    args.log.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(args.log, "w", newline="", encoding="utf-8")
    log = csv.writer(log_file)
    log.writerow(["sequence", "send_wall_time_ns", "receive_wall_time_ns", "one_way_ms", "bytes", "relayed"])
    if args.ready_file is not None:
        args.ready_file.parent.mkdir(parents=True, exist_ok=True)
        args.ready_file.write_text(f"{time.time_ns()}\n", encoding="utf-8")
    print(f"{time.time_ns() // 1_000_000} event=ue_client_ready bind={args.bind}", flush=True)

    while not stop:
        try:
            data, _ = udp.recvfrom(65536)
        except socket.timeout:
            continue
        except InterruptedError:
            continue
        receive_ns = time.time_ns()
        try:
            body = json.loads(data)
            sequence, send_ns = int(body["sequence"]), int(body["send_wall_time_ns"])
        except (ValueError, KeyError, TypeError):
            sequence, send_ns = -1, 0
        try:
            relay.sendto(messages.pack_relay(receive_ns, data), str(args.relay_socket))
            relayed = 1
        except (FileNotFoundError, ConnectionRefusedError):
            relayed = 0
        log.writerow([sequence, send_ns, receive_ns,
                      round((receive_ns - send_ns) / 1e6, 3) if send_ns else "", len(data), relayed])
        log_file.flush()
    log_file.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
