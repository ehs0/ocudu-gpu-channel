#!/usr/bin/env python3
"""Replay the offered-traffic trace on the broker's sample clock.

Roles (single-cell mode; the legacy traffic.py is unchanged):

  dl-send   root namespace: every DL packet of the trace, to its UE.
  ul-send   one UE's namespace: that UE's UL packets, to the gateway.
  dl-recv / ul-recv   traffic.run_receiver on the single-cell ports.

A sender waits for the replay bridge's epoch file (measurement sample S0),
then polls the broker's `link_clock` and releases each packet once the link
that carries it (gnb0>ueI for DL, ueI>gnb0 for UL) has reached sample
S0 + round(t * fs). Pacing on that clock, not on wall time, is what makes a
slow run (the GB10 radios run at 0.4-0.65x real time) see the same offered
load per radio second as a fast one.

Log, per sent packet (struct SENT): flow id, seq, target sample, the link
sample observed when it was sent, and the send wall time. The difference
observed - target is the injection lag; validation.py grades it.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import signal
import socket
import struct
import sys
import time
from typing import Any, Sequence

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import definitions as bench  # noqa: E402
import replay_bridge  # noqa: E402
import traffic  # noqa: E402
import traffic_trace  # noqa: E402

SENT = struct.Struct("<HIqqq")  # flow_id, seq, target_sample, observed_sample, tx_unix_ns
MODEL = "sionna_rt"


def carrier_link(direction: int, ue_index: int) -> str:
    return f"gnb0>ue{ue_index}:{MODEL}" if direction == 0 else f"ue{ue_index}>gnb0:{MODEL}"


def wait_for(path: pathlib.Path, stop: list[bool]) -> dict[str, Any] | None:
    while not stop[0]:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        time.sleep(0.01)
    return None


def run_sender(args: argparse.Namespace, direction: int, ue_index: int | None) -> int:
    stop = [False]
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__(0, True))
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__(0, True))
    verification = traffic_trace.verify(args.bundle)
    if not verification["ok"]:
        print(json.dumps({"event": "traffic_refused", "errors": verification["errors"]}), flush=True)
        return 3
    manifest = json.loads((args.bundle / traffic_trace.MANIFEST_FILE).read_text(encoding="utf-8"))
    flows = {flow["flow_id"]: flow for flow in manifest["flows"]}
    queues: dict[str, collections.deque] = {}
    for t, seq, fid, size, d, ue in traffic_trace.iter_records(args.bundle):
        if d != direction or (ue_index is not None and ue != ue_index):
            continue
        queues.setdefault(carrier_link(d, ue), collections.deque()).append((t, seq, fid, size, ue))
    total = sum(len(q) for q in queues.values())
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    traffic.big_buffers(sock, getattr(socket, "SO_SNDBUFFORCE", 32), socket.SO_SNDBUF)
    payloads: dict[int, bytes] = {}
    print(json.dumps({"event": "radio_traffic_sender_ready", "direction": traffic_trace.DIRECTIONS[direction],
                      "ue": ue_index, "packets": total, "links": sorted(queues),
                      "trace_sha256": verification["trace_sha256"]}), flush=True)
    epoch = wait_for(args.epoch_file, stop)
    if epoch is None:
        return 0
    s0 = int(epoch["s0"])
    fs = float(epoch["sample_rate_hz"])
    control = replay_bridge.Control(args.control_endpoint)
    sent = errors = polls = 0
    lags: list[int] = []
    pending = bytearray()
    last_flush = time.monotonic()
    with args.log.open("ab") as log:
        while queues and not stop[0]:
            clock = control.clock()
            polls += 1
            for link_id in list(queues):
                now_sample = clock[link_id]["sample"]
                queue = queues[link_id]
                while queue and s0 + round(queue[0][0] * fs) <= now_sample:
                    t, seq, fid, size, ue = queue.popleft()
                    target = s0 + round(t * fs)
                    flow = flows[fid]
                    address = (bench.single_cell_ue(ue)["ipv4"], flow["port"]) if direction == 0 else \
                        (bench.UE_GATEWAY, bench.UL_PORT)
                    if size not in payloads:
                        payloads[size] = bytes(size - traffic.HEADER.size)
                    tx_ns = time.time_ns()
                    try:
                        sock.sendto(traffic.HEADER.pack(traffic.MAGIC, fid, 0, seq, tx_ns) + payloads[size], address)
                        sent += 1
                    except OSError:
                        errors += 1
                    pending += SENT.pack(fid, seq, target, now_sample, tx_ns)
                    lags.append(now_sample - target)
                if not queue:
                    del queues[link_id]
            if time.monotonic() - last_flush >= 0.5 and pending:
                log.write(pending)
                log.flush()
                pending.clear()
                last_flush = time.monotonic()
            time.sleep(args.poll_ms / 1000.0)
        if pending:
            log.write(pending)
    lags.sort()
    print(json.dumps({
        "event": "radio_traffic_sender_done", "packets": sent, "send_errors": errors, "unsent": sum(len(q) for q in queues.values()),
        "polls": polls, "stopped": stop[0],
        "lag_samples": {"p50": lags[len(lags) // 2] if lags else None,
                        "p99": lags[min(len(lags) - 1, int(0.99 * len(lags)))] if lags else None,
                        "max": lags[-1] if lags else None}}), flush=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("role", choices=("dl-send", "ul-send", "dl-recv", "ul-recv"))
    parser.add_argument("--bundle", type=pathlib.Path, required=True, help="directory with the traffic trace")
    parser.add_argument("--log", type=pathlib.Path, required=True)
    parser.add_argument("--ue", help="ul-send / dl-recv: ueI")
    parser.add_argument("--epoch-file", type=pathlib.Path, help="senders: the replay bridge's epoch file")
    parser.add_argument("--control-endpoint", help="senders: broker control endpoint (link_clock)")
    parser.add_argument("--poll-ms", type=float, default=1.0)
    parser.add_argument("--until-file", type=pathlib.Path, help="receivers: stop when this file exists")
    args = parser.parse_args(argv)
    ue_index = int(args.ue[2:]) if args.ue else None
    if args.role in ("dl-send", "ul-send"):
        if args.epoch_file is None or args.control_endpoint is None:
            parser.error("senders need --epoch-file and --control-endpoint")
        if args.role == "ul-send" and ue_index is None:
            parser.error("ul-send needs --ue")
        return run_sender(args, 0 if args.role == "dl-send" else 1, None if args.role == "dl-send" else ue_index)
    manifest = json.loads((args.bundle / traffic_trace.MANIFEST_FILE).read_text(encoding="utf-8"))
    if args.role == "dl-recv":
        if ue_index is None:
            parser.error("dl-recv needs --ue")
        ports = [f["port"] for f in manifest["flows"] if f["direction"] == "dl" and f["ue_index"] == ue_index]
        if not ports:
            print(json.dumps({"event": "traffic_receiver_idle", "ue": args.ue}), flush=True)
            return 0
        return traffic.run_receiver("0.0.0.0", ports, args.log, args.until_file)
    return traffic.run_receiver(bench.UE_GATEWAY, [bench.UL_PORT], args.log, args.until_file)


if __name__ == "__main__":
    raise SystemExit(main())
