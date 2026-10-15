#!/usr/bin/env python3
"""One robot's UE link manager (stdlib only): offline queue, heartbeat, resume, ACK-based delivery.

In the live gate it runs inside the robot's UE network namespace, bound to the
UE's TUN address, so the only route to the mission server is the 5G user
plane through the channel emulator (that namespace has no other interface).
robot.py talks to it over a unix datagram socket (`--bus-socket`), which does
not cross the radio.

Link states (`LinkStateMachine`): BOOT -> ONLINE | OFFLINE; ONLINE -> DEGRADED
after `--degraded-timeout-s` without a valid reply from the server, -> OFFLINE
after `--offline-timeout-s`; OFFLINE -> SYNCING on a `resume_ack`; SYNCING ->
ONLINE once the backlog is acknowledged. The state is measured from replies
only: radio-side indicators (TUN interface present) are logged next to it but
never make the link ONLINE.

The manager never stops trying: while OFFLINE it sends `resume_hello` probes
with a bounded exponential backoff (`--retry-backoff-initial-s` doubling to
`--retry-backoff-max-s`). It does not restart the UE; srsUE's own recovery is
the radio-side mechanism and is measured, not assisted.

Outputs (`--log-dir`): `link_state.csv` (transitions), `delivered.csv` (one row
per acknowledged event: captured, enqueued, first send, ACK), `tasks.csv`,
`link.jsonl` (bind errors, purges, evictions); `--status-json` is rewritten
twice a second for the dashboard.
"""

from __future__ import annotations

import argparse
import csv
import errno
import json
import os
import pathlib
import select
import signal
import socket
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import messages  # noqa: E402
from offline_queue import OfflineQueue  # noqa: E402


class LinkStateMachine:
    """Application-link state from measured server replies, with hysteresis (pure; clock injected)."""

    def __init__(self, *, degraded_timeout_s=3.0, offline_timeout_s=5.0, recover_replies=2,
                 heartbeat_interval_s=1.0, backoff_initial_s=1.0, backoff_max_s=8.0, now=0.0):
        self.degraded_timeout_s = degraded_timeout_s
        self.offline_timeout_s = offline_timeout_s
        self.recover_replies = recover_replies
        self.heartbeat_interval_s = heartbeat_interval_s
        self.backoff_initial_s = backoff_initial_s
        self.backoff_max_s = backoff_max_s
        self.state = "BOOT"
        self.since = now
        self.boot_time = now
        self.last_reply: float | None = None
        self.replies_in_degraded = 0
        self.backoff_s = backoff_initial_s
        self.next_probe = now
        self.transitions: list[tuple[float, str, str, str]] = []

    def _go(self, now: float, state: str, reason: str) -> None:
        if state != self.state:
            self.transitions.append((now, self.state, state, reason))
            self.state = state
            self.since = now
            if state == "DEGRADED":
                self.replies_in_degraded = 0

    @property
    def needs_handshake(self) -> bool:
        return self.state in ("BOOT", "OFFLINE")

    @property
    def may_send_data(self) -> bool:
        return self.state in ("ONLINE", "DEGRADED", "SYNCING")

    def on_reply(self, now: float) -> None:
        """Any valid downlink datagram from the server."""

        self.last_reply = now
        self.backoff_s = self.backoff_initial_s
        if self.state == "DEGRADED":
            self.replies_in_degraded += 1
            if self.replies_in_degraded >= self.recover_replies:
                self._go(now, "ONLINE", "replies_resumed")

    def on_resume_ack(self, now: float, backlog: int) -> None:
        self.on_reply(now)
        if self.state in ("BOOT", "OFFLINE"):
            if backlog > 0:
                self._go(now, "SYNCING", "resume_ack")
            else:
                self._go(now, "ONLINE", "resume_ack")

    def on_hard_disconnect(self, now: float, reason: str) -> None:
        if self.state != "BOOT":
            self._go(now, "OFFLINE", reason)

    def tick(self, now: float, backlog: int) -> None:
        age = None if self.last_reply is None else now - self.last_reply
        if self.state == "BOOT":
            if now - self.boot_time >= self.offline_timeout_s:
                self._go(now, "OFFLINE", "no_initial_handshake")
        elif self.state == "ONLINE":
            if age is not None and age >= self.offline_timeout_s:
                self._go(now, "OFFLINE", "reply_timeout")
            elif age is not None and age >= self.degraded_timeout_s:
                self._go(now, "DEGRADED", "reply_timeout")
        elif self.state == "DEGRADED":
            if age is not None and age >= self.offline_timeout_s:
                self._go(now, "OFFLINE", "reply_timeout")
        elif self.state == "SYNCING":
            if age is not None and age >= self.offline_timeout_s:
                self._go(now, "OFFLINE", "reply_timeout")
            elif backlog == 0:
                self._go(now, "ONLINE", "backlog_acked")

    def probe_due(self, now: float) -> str | None:
        """'resume_hello' / 'heartbeat' when a probe is due now, else None; schedules the next one."""

        if now < self.next_probe:
            return None
        if self.needs_handshake:
            self.next_probe = now + self.backoff_s
            self.backoff_s = min(self.backoff_s * 2.0, self.backoff_max_s)
            return "resume_hello"
        self.next_probe = now + self.heartbeat_interval_s
        return "heartbeat"


class RttEstimator:
    """RFC 6298-style smoothed RTT for the retransmission timeout."""

    def __init__(self, *, initial_rto_ms=1500, min_rto_ms=400, max_rto_ms=6000):
        self.srtt = None
        self.rttvar = None
        self.min_rto_ms = min_rto_ms
        self.max_rto_ms = max_rto_ms
        self.initial_rto_ms = initial_rto_ms

    def sample(self, rtt_ms: float) -> None:
        if self.srtt is None:
            self.srtt, self.rttvar = rtt_ms, rtt_ms / 2
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - rtt_ms)
            self.srtt = 0.875 * self.srtt + 0.125 * rtt_ms

    @property
    def rto_ms(self) -> int:
        if self.srtt is None:
            return self.initial_rto_ms
        return int(min(max(self.srtt + 4 * self.rttvar, self.min_rto_ms), self.max_rto_ms))


def _write_json_atomic(path: pathlib.Path, body: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--robot-id", required=True)
    ap.add_argument("--bind", required=True, help="ip:port (the UE's TUN address in the live gate)")
    ap.add_argument("--server", required=True, help="mission server ip:port")
    ap.add_argument("--bus-socket", type=pathlib.Path, required=True, help="unix socket robot.py sends to")
    ap.add_argument("--queue-db", type=pathlib.Path, required=True)
    ap.add_argument("--queue-max-mib", type=float, default=128.0)
    ap.add_argument("--log-dir", type=pathlib.Path, required=True)
    ap.add_argument("--status-json", type=pathlib.Path)
    ap.add_argument("--ready-file", type=pathlib.Path)
    ap.add_argument("--iface", help="radio interface to watch (tun_srsue); logged, never decides ONLINE")
    ap.add_argument("--heartbeat-interval-s", type=float, default=1.0)
    ap.add_argument("--degraded-timeout-s", type=float, default=3.0)
    ap.add_argument("--offline-timeout-s", type=float, default=5.0)
    ap.add_argument("--retry-backoff-initial-s", type=float, default=1.0)
    ap.add_argument("--retry-backoff-max-s", type=float, default=8.0)
    ap.add_argument("--window", type=int, default=24, help="max events in flight")
    ap.add_argument("--max-datagrams-per-s", type=float, default=40.0)
    args = ap.parse_args(argv)

    robot_id = args.robot_id
    bind_host, _, bind_port = args.bind.rpartition(":")
    server_host, _, server_port = args.server.rpartition(":")
    server = (server_host, int(server_port))
    boot_ms = messages.now_ms()
    session_id = f"{robot_id}-s{boot_ms}"
    mono = time.monotonic

    args.log_dir.mkdir(parents=True, exist_ok=True)
    args.queue_db.parent.mkdir(parents=True, exist_ok=True)
    queue = OfflineQueue(args.queue_db, max_bytes=int(args.queue_max_mib * 1024 * 1024))
    jsonl = open(args.log_dir / "link.jsonl", "a", encoding="utf-8")

    def note(event: str, **fields) -> None:
        jsonl.write(json.dumps({"t_ms": messages.now_ms(), "event": event, **fields}) + "\n")
        jsonl.flush()

    state_file = open(args.log_dir / "link_state.csv", "a", newline="", encoding="utf-8")
    state_csv = csv.writer(state_file)
    if state_file.tell() == 0:
        state_csv.writerow(["t_ms", "robot_id", "from", "to", "reason", "backlog", "iface_up"])
    delivered_file = open(args.log_dir / "delivered.csv", "a", newline="", encoding="utf-8")
    delivered_csv = csv.writer(delivered_file)
    if delivered_file.tell() == 0:
        delivered_csv.writerow(["robot_id", "event_id", "type", "priority", "captured_at_ms", "enqueued_ms",
                                "first_send_ms", "acked_ms", "send_count", "bytes"])
    status_file = open(args.log_dir / "link_status.csv", "a", newline="", encoding="utf-8")
    status_csv = csv.writer(status_file)
    if status_file.tell() == 0:
        status_csv.writerow(["t_ms", "robot_id", "state", "backlog", "backlog_bytes", "in_flight",
                             "last_reply_age_s", "rto_ms", "socket_bound", "iface_up", "evicted_total"])
    last_status_row = 0.0
    tasks_file = open(args.log_dir / "tasks.csv", "a", newline="", encoding="utf-8")
    tasks_csv = csv.writer(tasks_file)
    if tasks_file.tell() == 0:
        tasks_csv.writerow(["received_ms", "command_id", "revision", "issued_at_ms", "valid_until_ms",
                            "forwarded"])

    # Robot bus.
    if args.bus_socket.exists():
        args.bus_socket.unlink()
    bus = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    bus.bind(str(args.bus_socket))
    bus.setblocking(False)
    robot_address: str | None = None

    udp: socket.socket | None = None
    next_bind = 0.0

    def try_bind(now: float) -> socket.socket | None:
        nonlocal next_bind
        if now < next_bind:
            return None
        next_bind = now + 1.0
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((bind_host, int(bind_port)))
        except OSError as exc:
            sock.close()
            note("bind_error", errno=exc.errno, error=str(exc))
            return None
        sock.setblocking(False)
        note("bound", address=args.bind)
        return sock

    sm = LinkStateMachine(degraded_timeout_s=args.degraded_timeout_s, offline_timeout_s=args.offline_timeout_s,
                          heartbeat_interval_s=args.heartbeat_interval_s,
                          backoff_initial_s=args.retry_backoff_initial_s,
                          backoff_max_s=args.retry_backoff_max_s, now=mono())
    rtt = RttEstimator()
    mission_id: str | None = None
    link_seq = 0
    counters = {"datagrams_tx": 0, "bytes_tx": 0, "datagrams_rx": 0, "bytes_rx": 0, "events_sent": 0,
                "events_retransmitted": 0, "events_acked": 0, "events_in": 0, "events_invalid": 0,
                "tasks_rx": 0, "send_errors": 0, "rx_invalid": 0, "evicted": 0, "probes": 0}
    tokens = args.max_datagrams_per_s
    last_token = mono()
    reported_transitions = 0
    iface_up: bool | None = None
    last_status = 0.0
    stop = False

    def on_signal(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    def send_robot(body: dict) -> None:
        if robot_address is None:
            return
        try:
            bus.sendto(messages.dumps(body), robot_address)
        except OSError:
            pass   # robot.py not running (between episodes); it re-learns state from the next update

    def send_udp(data: bytes) -> bool:
        nonlocal udp
        if udp is None:
            return False
        try:
            udp.sendto(data, server)
        except OSError as exc:
            counters["send_errors"] += 1
            if exc.errno in (errno.EADDRNOTAVAIL, errno.ENETUNREACH, errno.ENODEV, errno.EINVAL):
                note("socket_lost", errno=exc.errno, error=str(exc))
                udp.close()
                udp = None
                sm.on_hard_disconnect(mono(), "socket_lost")
            return False
        counters["datagrams_tx"] += 1
        counters["bytes_tx"] += len(data)
        return True

    def enqueue_link_event(type_: str, payload: dict) -> None:
        nonlocal link_seq
        if mission_id is None:
            return
        link_seq += 1
        event = messages.make_event(mission_id=mission_id, robot_id=robot_id, boot=f"L{boot_ms}",
                                    seq=link_seq, type_=type_, captured_at_ms=messages.now_ms(),
                                    pose=None, payload=payload)
        counters["evicted"] += queue.put(event, priority=messages.priority(event), coalesce_key=None,
                                         now_ms=messages.now_ms())

    if args.ready_file is not None:
        args.ready_file.parent.mkdir(parents=True, exist_ok=True)
        args.ready_file.write_text(f"{messages.now_ms()}\n", encoding="utf-8")
    print(f"{messages.now_ms()} event=ue_link_ready robot={robot_id} bind={args.bind} server={args.server}",
          flush=True)

    while not stop:
        now = mono()
        if udp is None:
            udp = try_bind(now)
        if args.iface:
            # if_nameindex() asks this network namespace; /sys/class/net would show the
            # namespace sysfs was mounted in (the parent's, under `nsenter --net`).
            up = any(name == args.iface for _, name in socket.if_nameindex())
            if up != iface_up:
                note("iface", name=args.iface, up=up)
                iface_up = up

        readable = [bus] + ([udp] if udp is not None else [])
        try:
            ready, _, _ = select.select(readable, [], [], 0.05)
        except InterruptedError:
            continue
        now = mono()

        if bus in ready:
            while True:
                try:
                    data, address = bus.recvfrom(65536)
                except (BlockingIOError, InterruptedError):
                    break
                robot_address = address or robot_address
                try:
                    body = messages.loads(data)
                    if body.get("kind") == "hello":
                        robot_address = body.get("reply_to", robot_address)
                        continue
                    if body.get("kind") != "event":
                        raise ValueError("unknown bus kind")
                    event = messages.validate_event(body["event"])
                except (ValueError, KeyError) as exc:
                    counters["events_invalid"] += 1
                    note("bus_invalid", error=str(exc))
                    continue
                if event["robot_id"] != robot_id:
                    counters["events_invalid"] += 1
                    continue
                if event["mission_id"] != mission_id:
                    purged = queue.purge_other_missions(event["mission_id"])
                    note("mission", mission_id=event["mission_id"], purged_previous_mission=purged)
                    mission_id = event["mission_id"]
                counters["events_in"] += 1
                counters["evicted"] += queue.put(event, priority=messages.priority(event),
                                                 coalesce_key=messages.coalesce_key(event),
                                                 now_ms=messages.now_ms())

        if udp is not None and udp in ready:
            while udp is not None:
                try:
                    data, _ = udp.recvfrom(65536)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError as exc:
                    note("recv_error", errno=exc.errno, error=str(exc))
                    break
                counters["datagrams_rx"] += 1
                counters["bytes_rx"] += len(data)
                try:
                    body = messages.parse_downlink(data)
                except ValueError:
                    counters["rx_invalid"] += 1
                    continue
                if body["robot_id"] != robot_id:
                    counters["rx_invalid"] += 1
                    continue
                now_wall = messages.now_ms()
                kind = body["kind"]
                if kind in ("heartbeat_ack", "resume_ack") and isinstance(body.get("echo_ms"), int):
                    rtt.sample(max(0, now_wall - body["echo_ms"]))
                if kind == "resume_ack":
                    sm.on_resume_ack(now, queue.count())
                else:
                    sm.on_reply(now)
                if kind == "ack":
                    for item in queue.ack([str(x) for x in body.get("event_ids", [])], now_wall):
                        counters["events_acked"] += 1
                        delivered_csv.writerow([robot_id, item.event_id, item.type, item.priority,
                                                item.captured_at_ms, item.enqueued_ms, item.first_send_ms,
                                                item.acked_ms, item.send_count, item.size])
                    delivered_file.flush()
                elif kind == "task":
                    counters["tasks_rx"] += 1
                    task = body["task"]
                    forwarded = robot_address is not None
                    send_robot({"kind": "task", "task": task, "received_ms": now_wall})
                    tasks_csv.writerow([now_wall, task["command_id"], task["revision"], task["issued_at_ms"],
                                        task["valid_until_ms"], int(forwarded)])
                    tasks_file.flush()

        backlog = queue.count()
        sm.tick(now, backlog)

        probe = sm.probe_due(now)
        if probe is not None and udp is not None:
            counters["probes"] += 1
            send_udp(messages.uplink(probe, robot_id=robot_id, session_id=session_id,
                                     sent_ms=messages.now_ms(), state=sm.state, backlog=backlog,
                                     mission_id=mission_id))

        # Data: only while the server answers; paced, windowed, priority first.
        if sm.may_send_data and udp is not None:
            tokens = min(args.max_datagrams_per_s, tokens + (now - last_token) * args.max_datagrams_per_s)
            last_token = now
            wall = messages.now_ms()
            room = args.window - queue.in_flight(now_ms=wall, rto_ms=rtt.rto_ms)
            while tokens >= 1.0 and room > 0:
                batch = queue.next_batch(now_ms=wall, rto_ms=rtt.rto_ms, limit=min(room, 16))
                if not batch:
                    break
                data, included = messages.pack_events(batch, robot_id=robot_id, session_id=session_id,
                                                       sent_ms=wall)
                if not send_udp(data):
                    break
                ids = [event["event_id"] for event in included]
                queue.mark_sent(ids, wall)
                counters["events_sent"] += len(ids)
                tokens -= 1.0
                room -= len(ids)
        else:
            last_token = now

        while reported_transitions < len(sm.transitions):
            t, old, new, reason = sm.transitions[reported_transitions]
            reported_transitions += 1
            wall = messages.now_ms()
            state_csv.writerow([wall, robot_id, old, new, reason, backlog, "" if iface_up is None else int(iface_up)])
            state_file.flush()
            enqueue_link_event("app_link_state", {"from": old, "to": new, "reason": reason,
                                                  "backlog": backlog})
            send_robot({"kind": "link_state", "state": new, "since_ms": wall, "backlog": backlog})

        if now - last_status >= 0.5:
            last_status = now
            wall = messages.now_ms()
            age = None if sm.last_reply is None else round(now - sm.last_reply, 3)
            status = {"t_ms": wall, "robot_id": robot_id, "state": sm.state,
                      "state_age_s": round(now - sm.since, 3), "last_reply_age_s": age,
                      "rto_ms": rtt.rto_ms, "srtt_ms": None if rtt.srtt is None else round(rtt.srtt, 1),
                      "backlog": backlog, "backlog_bytes": queue.bytes(), "backlog_by_type": queue.counts_by_type(),
                      "evicted_total": queue.evicted, "socket_bound": udp is not None,
                      "iface_up": iface_up, "mission_id": mission_id, "next_probe_in_s": round(sm.next_probe - now, 2),
                      "counters": counters}
            send_robot({"kind": "link_state", "state": sm.state, "since_ms": wall - int((now - sm.since) * 1000),
                        "backlog": backlog})
            if args.status_json is not None:
                _write_json_atomic(args.status_json, status)
            if now - last_status_row >= 1.0:
                last_status_row = now
                status_csv.writerow([wall, robot_id, sm.state, backlog, status["backlog_bytes"],
                                     queue.in_flight(now_ms=wall, rto_ms=rtt.rto_ms), age, rtt.rto_ms,
                                     int(udp is not None), "" if iface_up is None else int(iface_up), queue.evicted])
                status_file.flush()

    note("stop", counters=counters, backlog=queue.count())
    queue.close()
    for handle in (jsonl, state_file, delivered_file, tasks_file, status_file):
        handle.close()
    try:
        args.bus_socket.unlink()
    except FileNotFoundError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
