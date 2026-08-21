#!/usr/bin/env python3
"""Read-only web status for the external Sionna/OCUDU channel path.

The server subscribes to the broker telemetry PUB socket and tails the JSONL
records emitted by ``scripts/sionna_rt/run_bridge.py``. It never connects to
the broker control REP socket and therefore cannot mutate a live channel.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import pathlib
import signal
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Sequence


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_INDEX = pathlib.Path(__file__).with_name("index.html")


def process_is_alive(pid: Any) -> bool:
    """Return whether a reported producer PID still exists."""

    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def parse_nvidia_pmon(output: str) -> list[dict[str, Any]]:
    """Parse the header-driven `nvidia-smi pmon` per-process sample."""

    header: list[str] | None = None
    rows: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            fields = line.lstrip("# ").lower().split()
            if "pid" in fields and ("gpu" in fields or "idx" in fields):
                header = fields
            continue
        if header is None:
            continue
        values = line.split()
        if len(values) < len(header):
            continue
        item = dict(zip(header, values))
        try:
            pid = int(item["pid"])
            gpu = int(item.get("gpu", item.get("idx", "-1")))
        except (KeyError, ValueError):
            continue

        def percentage(name: str) -> float | None:
            value = item.get(name, "-")
            try:
                return float(value) if value not in ("-", "N/A") else None
            except ValueError:
                return None

        rows.append(
            {
                "pid": pid,
                "gpu_index": gpu,
                "type": item.get("type"),
                "sm_util_percent": percentage("sm"),
                "memory_util_percent": percentage("mem"),
                "gpu_memory_mib": percentage("fb"),
                "command": item.get("command"),
            }
        )
    return rows


def parse_nvidia_gpu_map(output: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in csv.reader(io.StringIO(output)):
        if len(row) < 2:
            continue
        try:
            result[row[1].strip()] = int(row[0].strip())
        except ValueError:
            continue
    return result


def parse_nvidia_compute_apps(
    output: str, gpu_indices: dict[str, int]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in csv.reader(io.StringIO(output)):
        if len(row) < 4:
            continue
        gpu_uuid, pid_text, process_name, memory_text = (value.strip() for value in row[:4])
        try:
            pid = int(pid_text)
            memory_mib = float(memory_text)
        except ValueError:
            continue
        rows.append(
            {
                "pid": pid,
                "gpu_uuid": gpu_uuid,
                "gpu_index": gpu_indices.get(gpu_uuid),
                "process_name": process_name,
                "gpu_memory_mib": memory_mib,
            }
        )
    return rows


def sample_gpu_usage(targets: dict[str, set[int]]) -> dict[str, Any]:
    """Sample per-PID GPU residency and utilization for the two producers."""

    errors: list[str] = []

    def run_query(arguments: list[str]) -> str:
        try:
            completed = subprocess.run(
                arguments,
                check=False,
                capture_output=True,
                text=True,
                timeout=4.0,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc))
            return ""
        if completed.returncode != 0:
            errors.append(completed.stderr.strip() or f"{' '.join(arguments)} failed")
            return ""
        return completed.stdout

    pmon = parse_nvidia_pmon(
        run_query(["nvidia-smi", "pmon", "-c", "1", "-s", "um"])
    )

    processes: dict[str, Any] = {}
    for role in ("sionna", "gpu_channel"):
        pids = sorted(targets.get(role, set()))
        per_gpu: dict[Any, dict[str, Any]] = {}
        for item in pmon:
            if item["pid"] not in pids:
                continue
            key = item["gpu_index"]
            gpu = per_gpu.setdefault(
                key,
                {
                    "gpu_index": item["gpu_index"],
                    "gpu_uuid": None,
                    "gpu_memory_mib": 0.0,
                    "sm_util_percent": 0.0,
                    "memory_util_percent": 0.0,
                },
            )
            gpu["gpu_memory_mib"] += item["gpu_memory_mib"] or 0.0
            gpu["sm_util_percent"] += item["sm_util_percent"] or 0.0
            gpu["memory_util_percent"] += item["memory_util_percent"] or 0.0
        gpu_rows = sorted(
            per_gpu.values(),
            key=lambda item: (
                item["gpu_index"] is None,
                item["gpu_index"] if item["gpu_index"] is not None else 9999,
            ),
        )
        processes[role] = {
            "running": any(process_is_alive(pid) for pid in pids),
            "gpu_resident": bool(gpu_rows),
            "pids": pids,
            "gpu_count": len(gpu_rows),
            "gpu_memory_mib": sum(item["gpu_memory_mib"] for item in gpu_rows),
            "sm_util_percent": sum(item["sm_util_percent"] for item in gpu_rows),
            "memory_util_percent": sum(item["memory_util_percent"] for item in gpu_rows),
            "gpus": gpu_rows,
        }
    return {
        "sampled_unix_ms": time.time_ns() // 1_000_000,
        "available": not errors,
        "error": "; ".join(dict.fromkeys(errors)) if errors else None,
        "processes": processes,
    }


def parse_telemetry_frame(frame: str) -> tuple[str, dict[str, Any]]:
    """Split ``<link topic> <JSON>`` and validate the broker payload."""

    try:
        topic, body = frame.split(" ", 1)
        payload = json.loads(body)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid telemetry frame") from exc
    if not topic or not isinstance(payload, dict):
        raise ValueError("invalid telemetry frame")
    if payload.get("event") != "telemetry":
        raise ValueError("unexpected telemetry event")
    if payload.get("link_id") != topic:
        raise ValueError("telemetry topic/link_id mismatch")
    return topic, payload


def delivery_status(
    sionna: dict[str, Any] | None,
    telemetry: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Correlate one Sionna batch ACK with backend-applied seqnos.

    The control REP proves that the C++ control server accepted the batch.
    A telemetry seqno at or beyond the ACK's per-link seqno, together with an
    active profile and matching backend name, proves the data-plane snap path
    has observed it. This avoids treating an unrelated telemetry frame as
    confirmation of the latest Sionna update.
    """

    empty = {
        "state": "waiting_for_sionna",
        "batch_id": None,
        "backend": None,
        "control_received": False,
        "control_received_links": 0,
        "expected_links": 0,
        "backend_applied": False,
        "backend_applied_links": 0,
        "backend_usable": False,
        "backend_usable_links": 0,
        "warmup_links": 0,
        "links": [],
    }
    if not sionna:
        return empty

    reply = sionna.get("control_reply")
    if not isinstance(reply, dict):
        return {**empty, "state": "missing_control_reply"}
    batch_id = reply.get("batch_id") or sionna.get("batch_id")
    if reply.get("dry_run"):
        return {**empty, "state": "dry_run", "batch_id": batch_id}
    if not reply.get("ok"):
        return {**empty, "state": "control_rejected", "batch_id": batch_id}

    raw_channels = sionna.get("channels")
    if not isinstance(raw_channels, list):
        raw_channels = []
    channel_ids = {
        item.get("link_id")
        for item in raw_channels
        if isinstance(item, dict) and isinstance(item.get("link_id"), str)
    }
    acknowledged: dict[str, int] = {}
    acknowledged_warmup: dict[str, int] = {}
    raw_links = reply.get("links")
    if not isinstance(raw_links, list):
        raw_links = []
    for item in raw_links:
        if not isinstance(item, dict):
            continue
        link_id = item.get("link_id")
        seqno = item.get("seqno")
        if isinstance(link_id, str) and isinstance(seqno, int):
            acknowledged[link_id] = seqno
            warmup_until_slot = item.get("warmup_until_slot")
            if isinstance(warmup_until_slot, int):
                acknowledged_warmup[link_id] = warmup_until_slot

    expected_count = len(channel_ids) if channel_ids else len(acknowledged)
    control_received = (
        expected_count > 0
        and channel_ids.issubset(acknowledged)
        and reply.get("link_count") == expected_count
    )
    backend = reply.get("backend") if isinstance(reply.get("backend"), str) else None
    link_rows: list[dict[str, Any]] = []
    applied_count = 0
    usable_count = 0
    warmup_count = 0
    for link_id in sorted(channel_ids or acknowledged.keys()):
        expected_seqno = acknowledged.get(link_id)
        observed = telemetry.get(link_id, {})
        observed_seqno = observed.get("seqno")
        observed_backend = observed.get("backend")
        backend_matches = (
            backend in (None, "unknown")
            or observed_backend == backend
        )
        applied = (
            expected_seqno is not None
            and isinstance(observed_seqno, int)
            and observed_seqno >= expected_seqno
            and observed.get("profile_active") is True
            and backend_matches
        )
        if applied:
            applied_count += 1
        observed_slot = observed.get("slot")
        warmup_until_slot = observed.get("warmup_until_slot")
        if not isinstance(warmup_until_slot, int) or warmup_until_slot <= 0:
            warmup_until_slot = acknowledged_warmup.get(link_id, 0)
        warming_up = (
            applied
            and isinstance(observed_slot, int)
            and isinstance(warmup_until_slot, int)
            and warmup_until_slot > 0
            and observed_slot < warmup_until_slot
        )
        if warming_up:
            warmup_count += 1
        elif applied:
            usable_count += 1
        link_rows.append(
            {
                "link_id": link_id,
                "expected_seqno": expected_seqno,
                "observed_seqno": observed_seqno,
                "observed_slot": observed_slot,
                "profile_active": observed.get("profile_active") is True,
                "backend": observed_backend,
                "applied": applied,
                "warming_up": warming_up,
                "warmup_until_slot": warmup_until_slot,
                "usable": applied and not warming_up,
            }
        )

    backend_applied = (
        control_received
        and expected_count > 0
        and applied_count == expected_count
    )
    backend_usable = (
        backend_applied
        and usable_count == expected_count
        and warmup_count == 0
    )
    if not acknowledged:
        state = "accepted_without_seqnos"
    elif not control_received:
        state = "partial_control_ack"
    elif backend_applied and not backend_usable:
        state = "backend_warmup"
    elif backend_usable:
        state = "backend_applied"
    elif applied_count:
        state = "partially_applied"
    else:
        state = "awaiting_backend_telemetry"
    return {
        "state": state,
        "batch_id": batch_id,
        "backend": backend,
        "control_received": control_received,
        "control_received_links": len(acknowledged),
        "expected_links": expected_count,
        "backend_applied": backend_applied,
        "backend_applied_links": applied_count,
        "backend_usable": backend_usable,
        "backend_usable_links": usable_count,
        "warmup_links": warmup_count,
        "links": link_rows,
    }


class StatusStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_monotonic = time.monotonic()
        self._telemetry: dict[str, dict[str, Any]] = {}
        self._telemetry_seen: dict[str, float] = {}
        self._sionna: dict[str, Any] | None = None
        self._sionna_seen: float | None = None
        self._sionna_runtime: dict[str, Any] | None = None
        self._sionna_runtime_seen: float | None = None
        self._gpu_usage: dict[str, Any] | None = None
        self._gpu_history: list[dict[str, Any]] = []
        self._iteration_history: list[dict[str, Any]] = []
        self._bad_telemetry_frames = 0

    def update_telemetry(self, link_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._telemetry[link_id] = payload
            self._telemetry_seen[link_id] = time.monotonic()

    def note_bad_telemetry(self) -> None:
        with self._lock:
            self._bad_telemetry_frames += 1

    def update_sionna(self, payload: dict[str, Any]) -> None:
        with self._lock:
            if payload.get("event") == "sionna_rt_update":
                self._sionna = payload
                self._sionna_seen = time.monotonic()
                iteration = payload.get("iteration")
                observed_unix_ms = payload.get("control_ack_unix_ms")
                if not isinstance(observed_unix_ms, int):
                    observed_unix_ms = payload.get("update_started_unix_ms")
                if not isinstance(observed_unix_ms, int):
                    observed_unix_ms = time.time_ns() // 1_000_000
                reply = payload.get("control_reply")
                reply_links = reply.get("links", []) if isinstance(reply, dict) else []
                event = {
                    "observed_unix_ms": observed_unix_ms,
                    "iteration": iteration,
                    "session_id": payload.get("session_id"),
                    "warmup_expected": any(
                        isinstance(item, dict)
                        and isinstance(item.get("warmup_until_slot"), int)
                        and item["warmup_until_slot"] > 0
                        for item in reply_links
                    ),
                }
                if not self._iteration_history or self._iteration_history[-1] != event:
                    self._iteration_history.append(event)
                    del self._iteration_history[:-512]
            elif payload.get("event") == "sionna_rt_runtime":
                self._sionna_runtime = payload
                self._sionna_runtime_seen = time.monotonic()

    def process_targets(self) -> dict[str, set[int]]:
        with self._lock:
            sionna_pid = (
                self._sionna_runtime.get("process_id")
                if self._sionna_runtime
                else None
            )
            channel_pids = {
                payload.get("process_id")
                for payload in self._telemetry.values()
                if isinstance(payload.get("process_id"), int)
            }
        return {
            "sionna": {sionna_pid} if isinstance(sionna_pid, int) else set(),
            "gpu_channel": channel_pids,
        }

    def update_gpu_usage(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._gpu_usage = payload
            roles: dict[str, Any] = {}
            raw_processes = payload.get("processes", {})
            for role in ("sionna", "gpu_channel"):
                process = raw_processes.get(role, {})
                roles[role] = {
                    "running": process.get("running") is True,
                    "gpu_resident": process.get("gpu_resident") is True,
                    "sm_util_percent": process.get("sm_util_percent"),
                    "memory_util_percent": process.get("memory_util_percent"),
                    "gpu_memory_mib": process.get("gpu_memory_mib"),
                }
            warmup_payloads = [
                item
                for item in self._telemetry.values()
                if isinstance(item.get("slot"), int)
                and isinstance(item.get("warmup_until_slot"), int)
                and item["warmup_until_slot"] > 0
                and item["slot"] < item["warmup_until_slot"]
            ]
            self._gpu_history.append(
                {
                    "sampled_unix_ms": payload.get("sampled_unix_ms"),
                    "iteration": self._sionna.get("iteration") if self._sionna else None,
                    "phase": (
                        self._sionna_runtime.get("phase")
                        if self._sionna_runtime
                        else None
                    ),
                    "warmup_link_count": len(warmup_payloads),
                    "warmup_until_slot": max(
                        (item["warmup_until_slot"] for item in warmup_payloads),
                        default=0,
                    ),
                    "roles": roles,
                }
            )
            del self._gpu_history[:-240]

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            telemetry = dict(self._telemetry)
            telemetry_ages = {
                link_id: round(now - seen, 3)
                for link_id, seen in self._telemetry_seen.items()
            }
            sionna = self._sionna
            sionna_age = (
                None if self._sionna_seen is None else round(now - self._sionna_seen, 3)
            )
            sionna_runtime = (
                dict(self._sionna_runtime) if self._sionna_runtime else None
            )
            sionna_runtime_age = (
                None
                if self._sionna_runtime_seen is None
                else round(now - self._sionna_runtime_seen, 3)
            )
            gpu_usage = dict(self._gpu_usage) if self._gpu_usage else None
            gpu_history = list(self._gpu_history)
            iteration_history = list(self._iteration_history)
            bad_frames = self._bad_telemetry_frames
        if sionna_runtime is not None:
            sionna_runtime["process_alive"] = process_is_alive(
                sionna_runtime.get("process_id")
            )
            sionna_runtime["phase_age_seconds"] = sionna_runtime_age
        if gpu_usage is not None:
            gpu_usage["history"] = gpu_history
            gpu_usage["iterations"] = iteration_history
        return {
            "event": "web_status",
            "server_uptime_seconds": round(now - self._started_monotonic, 3),
            "feeds": {
                "telemetry_connected": bool(telemetry),
                "telemetry_link_count": len(telemetry),
                "telemetry_age_seconds": telemetry_ages,
                "sionna_connected": sionna is not None,
                "sionna_age_seconds": sionna_age,
                "sionna_process_alive": bool(
                    sionna_runtime and sionna_runtime.get("process_alive")
                ),
                "bad_telemetry_frames": bad_frames,
            },
            "telemetry": telemetry,
            "sionna": sionna,
            "sionna_runtime": sionna_runtime,
            "gpu_usage": gpu_usage,
            "delivery": delivery_status(sionna, telemetry),
        }


class SionnaJsonlTail:
    def __init__(self, path: pathlib.Path, store: StatusStore) -> None:
        self.path = path
        self.store = store
        self._offset = 0

    def poll_once(self) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size < self._offset:
            self._offset = 0
        with self.path.open("r", encoding="utf-8") as handle:
            handle.seek(self._offset)
            for line in handle:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    self.store.update_sionna(payload)
            self._offset = handle.tell()

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(0.25):
            self.poll_once()


def telemetry_loop(endpoint: str, store: StatusStore, stop: threading.Event) -> None:
    try:
        import zmq  # type: ignore
    except ImportError as exc:  # pragma: no cover - dependency error path
        raise RuntimeError(
            "pyzmq is required; install scripts/sionna_rt/requirements.txt"
        ) from exc

    context = zmq.Context.instance()
    socket = context.socket(zmq.SUB)
    socket.setsockopt(zmq.LINGER, 0)
    socket.setsockopt_string(zmq.SUBSCRIBE, "")
    socket.connect(endpoint)
    poller = zmq.Poller()
    poller.register(socket, zmq.POLLIN)
    try:
        while not stop.is_set():
            events = dict(poller.poll(200))
            if events.get(socket) != zmq.POLLIN:
                continue
            try:
                topic, payload = parse_telemetry_frame(socket.recv_string())
            except (ValueError, UnicodeDecodeError):
                store.note_bad_telemetry()
                continue
            store.update_telemetry(topic, payload)
    finally:
        socket.close(linger=0)


def gpu_usage_loop(store: StatusStore, stop: threading.Event) -> None:
    """Refresh process-scoped nvidia-smi metrics without blocking HTTP polls."""

    while not stop.is_set():
        store.update_gpu_usage(sample_gpu_usage(store.process_targets()))
        if stop.wait(0.5):
            break


def make_handler(store: StatusStore, index_html: bytes) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ocudu-channel-status/1"

        def send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self.send_bytes(HTTPStatus.OK, index_html, "text/html; charset=utf-8")
                return
            if path in ("/api/status", "/healthz"):
                snapshot = store.snapshot()
                if path == "/healthz":
                    snapshot = {"ok": True, "feeds": snapshot["feeds"]}
                body = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
                self.send_bytes(HTTPStatus.OK, body, "application/json")
                return
            self.send_bytes(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain")

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return Handler


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--telemetry-endpoint", default="tcp://127.0.0.1:5560")
    parser.add_argument(
        "--status-jsonl",
        type=pathlib.Path,
        default=PROJECT_ROOT / "results" / "sionna-2gnb-2ue.jsonl",
    )
    parser.add_argument("--index", type=pathlib.Path, default=DEFAULT_INDEX)
    args = parser.parse_args(argv)
    if not (1 <= args.port <= 65535):
        parser.error("port must be in [1, 65535]")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    import sys

    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        import zmq  # type: ignore  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "pyzmq is required; install scripts/sionna_rt/requirements.txt"
        ) from exc
    index_html = args.index.read_bytes()
    store = StatusStore()
    stop = threading.Event()

    telemetry_thread = threading.Thread(
        target=telemetry_loop,
        args=(args.telemetry_endpoint, store, stop),
        name="telemetry-sub",
        daemon=True,
    )
    status_thread = threading.Thread(
        target=SionnaJsonlTail(args.status_jsonl, store).run,
        args=(stop,),
        name="sionna-jsonl-tail",
        daemon=True,
    )
    gpu_thread = threading.Thread(
        target=gpu_usage_loop,
        args=(store, stop),
        name="nvidia-process-monitor",
        daemon=True,
    )
    telemetry_thread.start()
    status_thread.start()
    gpu_thread.start()

    server = ThreadingHTTPServer((args.bind, args.port), make_handler(store, index_html))

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    print(
        json.dumps(
            {
                "event": "web_ui_start",
                "listen": f"http://{args.bind}:{args.port}",
                "telemetry_endpoint": args.telemetry_endpoint,
                "status_jsonl": str(args.status_jsonl),
                "read_only": True,
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        stop.set()
        server.server_close()
        telemetry_thread.join(timeout=1.0)
        status_thread.join(timeout=1.0)
        gpu_thread.join(timeout=5.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
