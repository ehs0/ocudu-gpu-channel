#!/usr/bin/env python3
"""Replay a frozen channel trace into the broker on the radio-time axis.

The single-cell counterpart of run_bridge.py: it never solves anything. It
verifies the bundle's hashes (and refuses to run on any mismatch -- there is
no fallback to a live solve), sends the attach channel before the radios
start, and, once the gate releases the measurement, schedules update k of
every link at measurement sample S0 + k * samples_per_update of that link's
IQ stream (batch_commit take_effect_at_sample; the broker splits its serve
call there, so the update starts on exactly that sample).

S0 is chosen when the measurement is released: the largest link clock plus a
lead, rounded up to a whole 1 ms slot of samples. It is written to the epoch
file, which the traffic senders wait for, so packets and channel share the
same sample origin.

The broker keeps one pending update per link, so update k+1 of a link is
committed only after the link reports update k applied (`link_clock`).
Links are pipelined independently: a link never waits for another link's
clock, which differs by the radios' buffering.

Evidence: the broker logs `event=control_sample_applied` with the tag
`k=<k>;sha=<first 16 hex of the request's SHA-256>`, the target and actual
sample and the digest of the profile it decoded; validation.py reads those.
This process records its own view (`--applied-log`): commit and acknowledge
wall times, the target, and any late commit.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import signal
import sys
import time
from typing import Any, Sequence

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import channel_trace  # noqa: E402

SLOT_SAMPLES = 23040  # 1 ms at 23.04 MS/s, 15 kHz SCS


class Control:
    """REQ client that sends pre-serialized request strings unchanged."""

    def __init__(self, endpoint: str, timeout_ms: int = 5000) -> None:
        import zmq

        self.zmq = zmq
        self.endpoint = endpoint
        self.context = zmq.Context.instance()
        self.socket = self.context.socket(zmq.REQ)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.setsockopt(zmq.RCVTIMEO, timeout_ms)
        self.socket.setsockopt(zmq.SNDTIMEO, timeout_ms)
        self.socket.connect(endpoint)

    def raw(self, text: str) -> dict[str, Any]:
        self.socket.send_string(text)
        reply = json.loads(self.socket.recv_string())
        if not reply.get("ok"):
            raise RuntimeError(f"broker rejected request: {reply}")
        return reply

    def request(self, message: dict[str, Any]) -> dict[str, Any]:
        return self.raw(json.dumps(message, separators=(",", ":")))

    def clock(self) -> dict[str, dict[str, int]]:
        return {row["link_id"]: row for row in self.request({"type": "link_clock"})["links"]}

    def commit_one(self, link_id: str, message: str, *, target: int = 0, tag: str = "",
                   noise_epoch: bool = False) -> dict[str, Any]:
        # The batch id is the one the stored request names: the request is sent
        # byte for byte, so the batch is opened under its id (one link at a time,
        # so reusing an id across links never collides with an open batch).
        bid = json.loads(message)["batch_id"]
        self.request({"type": "batch_begin", "id": bid})
        try:
            self.raw(message)
            commit: dict[str, Any] = {"type": "batch_commit", "id": bid}
            if target:
                commit["take_effect_at_sample"] = target
            if tag:
                commit["tag"] = tag
            if noise_epoch:
                commit["noise_epoch"] = True
            reply = self.request(commit)
        except Exception:
            try:
                self.request({"type": "batch_abort", "id": bid})
            except Exception:
                pass
            raise
        seqnos = {row["link_id"]: row["seqno"] for row in reply.get("links", [])}
        if link_id not in seqnos:
            raise RuntimeError(f"commit of {bid} did not name {link_id}: {reply}")
        return {"seqno": seqnos[link_id], "late_links": reply.get("late_links", 0)}


def write_atomic(path: pathlib.Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def tag_for(k: int | None, link_sha: str) -> str:
    return f"{'attach' if k is None else f'k={k:06d}'};sha={link_sha[:16]}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", type=pathlib.Path, required=True)
    parser.add_argument("--control-endpoint", required=True)
    parser.add_argument("--hold-until-file", type=pathlib.Path, required=True)
    parser.add_argument("--epoch-file", type=pathlib.Path, required=True)
    parser.add_argument("--applied-log", type=pathlib.Path, required=True)
    parser.add_argument("--lead-ms", type=float, default=300.0, help="radio-time lead between release and S0")
    parser.add_argument("--apply-timeout-s", type=float, default=60.0,
                        help="wall seconds a link may take to apply one update before the replay fails")
    parser.add_argument("--poll-ms", type=float, default=2.0)
    args = parser.parse_args(argv)

    stop = [False]
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__(0, True))
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__(0, True))

    def log(event: str, **fields: Any) -> None:
        print(json.dumps({"event": event, "unix_ms": time.time_ns() // 1_000_000, **fields},
                         separators=(",", ":")), flush=True)

    verification = channel_trace.verify(args.bundle)
    if not verification["ok"]:
        log("replay_refused", reason="channel trace failed verification", errors=verification["errors"])
        return 3
    manifest = json.loads((args.bundle / channel_trace.MANIFEST_FILE).read_text(encoding="utf-8"))
    config = manifest["config"]
    period = int(config["samples_per_update"])
    sample_rate = float(config["sample_rate_hz"])
    records = list(channel_trace.iter_records(args.bundle))
    attach, updates = records[0], records[1:]
    link_ids = list(attach["link_ids"])
    messages = {link_id: [] for link_id in link_ids}  # link -> [(message, sha)] per k
    for record in updates:
        if record["link_ids"] != link_ids:
            log("replay_refused", reason="link set changes inside the trace")
            return 3
        for link_id, text in zip(record["link_ids"], record["messages"]):
            messages[link_id].append((text, record["link_sha256"][link_id]))
    K = len(updates)
    log("replay_verified", trace_sha256=verification["trace_sha256"], updates=K, links=link_ids,
        samples_per_update=period)

    control = Control(args.control_endpoint)
    applied = args.applied_log.open("a", encoding="utf-8")

    def record(payload: dict[str, Any]) -> None:
        applied.write(json.dumps(payload, separators=(",", ":")) + "\n")
        applied.flush()

    clock = control.clock()
    missing = sorted(set(link_ids) - set(clock))
    if missing:
        log("replay_refused", reason="broker lacks trace links", missing=missing)
        return 3
    for link_id, text in zip(attach["link_ids"], attach["messages"]):
        sha = attach["link_sha256"][link_id]
        result = control.commit_one(link_id, text, tag=tag_for(None, sha))
        record({"record": "attach", "link_id": link_id, "sha": sha, "seqno": result["seqno"],
                "commit_unix_ns": time.time_ns()})
    log("replay_attach_committed", batch_sha256=attach["batch_sha256"])

    while not args.hold_until_file.exists():
        if stop[0]:
            return 0
        time.sleep(0.01)

    clock = control.clock()
    frontier = max(row["sample"] for row in clock.values())
    lead = int(math.ceil(args.lead_ms / 1000.0 * sample_rate))
    s0 = int(math.ceil((frontier + lead) / SLOT_SAMPLES) * SLOT_SAMPLES)
    epoch = {"s0": s0, "sample_rate_hz": sample_rate, "samples_per_update": period, "updates": K,
             "measure_samples": K * period, "frontier_at_release": frontier, "lead_samples": lead,
             "release_unix_ns": time.time_ns(), "links": link_ids,
             "trace_sha256": verification["trace_sha256"],
             "clock_at_release": {link_id: row["sample"] for link_id, row in clock.items()}}
    # Update 0 of every link (with the noise epoch) is committed before the
    # epoch file exists, so no sender can start ahead of the channel.
    next_k = {link_id: 0 for link_id in link_ids}
    pending: dict[str, tuple[int, int, int]] = {}  # link -> (k, seqno, commit_ns)

    def commit(link_id: str) -> None:
        k = next_k[link_id]
        text, sha = messages[link_id][k]
        target = s0 + k * period
        started = time.time_ns()
        result = control.commit_one(link_id, text, target=target, tag=tag_for(k, sha), noise_epoch=(k == 0))
        pending[link_id] = (k, result["seqno"], started)
        next_k[link_id] = k + 1
        record({"record": "commit", "link_id": link_id, "k": k, "target_sample": target, "sha": sha,
                "seqno": result["seqno"], "late": result["late_links"] > 0, "commit_unix_ns": started,
                "ack_unix_ns": time.time_ns()})
        if result["late_links"]:
            log("replay_late_commit", link_id=link_id, k=k, target_sample=target)

    for link_id in link_ids:
        commit(link_id)
    write_atomic(args.epoch_file, epoch)
    log("replay_measure_epoch", **{key: epoch[key] for key in ("s0", "samples_per_update", "updates", "lead_samples")})

    waiting_since = {link_id: time.monotonic() for link_id in link_ids}
    end_sample = s0 + K * period
    status = 0
    complete_logged = False
    while not stop[0]:
        clock = control.clock()
        now = time.monotonic()
        for link_id in link_ids:
            if link_id not in pending:
                continue
            k, seqno, commit_ns = pending[link_id]
            row = clock[link_id]
            if row["applied_seqno"] >= seqno:
                record({"record": "applied", "link_id": link_id, "k": k, "seqno": seqno,
                        "observed_unix_ns": time.time_ns(), "observed_sample": row["sample"]})
                del pending[link_id]
                waiting_since[link_id] = now
                if next_k[link_id] < K:
                    commit(link_id)
            elif now - waiting_since[link_id] > args.apply_timeout_s:
                log("replay_stalled", link_id=link_id, k=k, link_sample=row["sample"],
                    target_sample=s0 + k * period)
                status = 4
                stop[0] = True
                break
        if not pending and not complete_logged:
            log("replay_all_applied", updates=K)
            complete_logged = True
        if complete_logged and all(row["sample"] >= end_sample for link_id, row in clock.items()
                                   if link_id in link_ids):
            log("replay_measure_end", end_sample=end_sample)
            break
        time.sleep(args.poll_ms / 1000.0)
    applied.close()
    # Keep the last channel applied (the broker holds it) and wait to be stopped,
    # so the gate decides when the radios stop, not this process.
    while not stop[0] and status == 0:
        time.sleep(0.2)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
