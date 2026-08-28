#!/usr/bin/env python3
"""Read-only web status for the external Sionna/OCUDU channel path.

The server subscribes to the broker telemetry PUB socket and tails the JSONL
records emitted by ``scripts/sionna_rt/run_bridge.py``. It never connects to
the broker control REP socket and therefore cannot mutate a live channel.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import ctypes.util
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
RESOURCE_SAMPLE_INTERVAL_SECONDS = 0.1
HARDWARE_REFRESH_INTERVAL_SECONDS = 1.0
INITIAL_JSONL_TAIL_BYTES = 4 * 1024 * 1024
RESOURCE_ROLES = ("sionna", "gpu_channel", "web_ui")


def read_text(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return ""


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
        gpu_uuid, pid_text, process_name, memory_text = (
            value.strip() for value in row[:4]
        )
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


def parse_nvidia_gpu_inventory(output: str) -> list[dict[str, Any]]:
    """Parse the no-units CSV used for device-wide GPU information."""

    rows: list[dict[str, Any]] = []
    for row in csv.reader(io.StringIO(output)):
        if len(row) < 7:
            continue
        try:
            gpu_index = int(row[0].strip())
            memory_total_mib = float(row[3].strip())
            memory_used_mib = float(row[4].strip())
        except ValueError:
            continue
        try:
            pcie_gen = int(row[5].strip())
        except ValueError:
            pcie_gen = None
        try:
            pcie_width = int(row[6].strip())
        except ValueError:
            pcie_width = None
        try:
            gpu_util_percent = float(row[7].strip())
        except (IndexError, ValueError):
            gpu_util_percent = None
        rows.append(
            {
                "gpu_index": gpu_index,
                "name": row[1].strip(),
                "gpu_uuid": row[2].strip(),
                "memory_total_mib": memory_total_mib,
                "memory_used_mib": memory_used_mib,
                "pcie_gen": pcie_gen,
                "pcie_width": pcie_width,
                "gpu_util_percent": gpu_util_percent,
            }
        )
    return rows


def probe_cuda_sm_counts() -> tuple[dict[int, int], str | None]:
    """Read SM counts with the CUDA driver API without creating a context."""

    library_name = ctypes.util.find_library("cuda") or "libcuda.so.1"
    try:
        cuda = ctypes.CDLL(library_name)
    except OSError as exc:
        return {}, str(exc)

    try:
        cuda.cuInit.argtypes = [ctypes.c_uint]
        cuda.cuInit.restype = ctypes.c_int
        cuda.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
        cuda.cuDeviceGetCount.restype = ctypes.c_int
        cuda.cuDeviceGet.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
        cuda.cuDeviceGet.restype = ctypes.c_int
        cuda.cuDeviceGetAttribute.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_int,
            ctypes.c_int,
        ]
        cuda.cuDeviceGetAttribute.restype = ctypes.c_int
    except AttributeError as exc:
        return {}, f"CUDA driver symbols unavailable: {exc}"

    status = cuda.cuInit(0)
    if status != 0:
        return {}, f"CUDA driver probe failed (cuInit={status})"
    count = ctypes.c_int()
    status = cuda.cuDeviceGetCount(ctypes.byref(count))
    if status != 0:
        return {}, f"CUDA device count failed ({status})"

    # CU_DEVICE_ATTRIBUTE_MULTIPROCESSOR_COUNT has value 16 in the CUDA API.
    sm_counts: dict[int, int] = {}
    for ordinal in range(count.value):
        device = ctypes.c_int()
        sm_count = ctypes.c_int()
        if cuda.cuDeviceGet(ctypes.byref(device), ordinal) != 0:
            continue
        if cuda.cuDeviceGetAttribute(ctypes.byref(sm_count), 16, device) == 0:
            sm_counts[ordinal] = sm_count.value
    return sm_counts, None


def parse_nvidia_dmon_pcie(output: str) -> dict[int, dict[str, float]]:
    """Parse device-wide PCIe Rx/Tx throughput from ``nvidia-smi dmon``."""

    header: list[str] | None = None
    result: dict[int, dict[str, float]] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            fields = line.lstrip("# ").lower().split()
            if ("gpu" in fields or "idx" in fields) and "rxpci" in fields:
                header = fields
            continue
        if header is None:
            continue
        values = line.split()
        if len(values) < len(header):
            continue
        item = dict(zip(header, values))
        try:
            gpu_index = int(item.get("gpu", item.get("idx", "-1")))
            rx_mb_s = float(item["rxpci"])
            tx_mb_s = float(item["txpci"])
        except (KeyError, ValueError):
            continue
        result[gpu_index] = {
            "h2d_mb_s": rx_mb_s,
            "d2h_mb_s": tx_mb_s,
        }
    return result


class _NvmlMemory(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


class _NvmlUtilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _NvmlProcessInfo(ctypes.Structure):
    _fields_ = [
        ("pid", ctypes.c_uint),
        ("used_gpu_memory", ctypes.c_ulonglong),
        ("gpu_instance_id", ctypes.c_uint),
        ("compute_instance_id", ctypes.c_uint),
    ]


class _NvmlProcessUtilizationSample(ctypes.Structure):
    _fields_ = [
        ("pid", ctypes.c_uint),
        ("timestamp", ctypes.c_ulonglong),
        ("sm_util", ctypes.c_uint),
        ("memory_util", ctypes.c_uint),
        ("encoder_util", ctypes.c_uint),
        ("decoder_util", ctypes.c_uint),
    ]


class NvmlResourceSampler:
    """Low-overhead NVML sampler for the resource timeline."""

    NVML_SUCCESS = 0
    NVML_ERROR_INSUFFICIENT_SIZE = 7
    NVML_PCIE_UTIL_TX_BYTES = 0
    NVML_PCIE_UTIL_RX_BYTES = 1
    NVML_VALUE_NOT_AVAILABLE = (1 << 64) - 1

    def __init__(self) -> None:
        library_name = ctypes.util.find_library("nvidia-ml") or "libnvidia-ml.so.1"
        try:
            self._nvml = ctypes.CDLL(library_name)
        except OSError as exc:
            raise RuntimeError(f"NVML unavailable: {exc}") from exc
        self._bind()
        status = self._nvml.nvmlInit_v2()
        if status != self.NVML_SUCCESS:
            raise RuntimeError(f"NVML initialization failed ({status})")
        self._closed = False
        self._handles: list[ctypes.c_void_p] = []
        self._inventory: list[dict[str, Any]] = []
        self._inventory_updated = 0.0
        self._last_process_timestamps: dict[int, int] = {}
        self._sm_by_process: dict[tuple[int, int], float] = {}
        self._sm_seen_monotonic: dict[tuple[int, int], float] = {}
        self._sm_counts, self._sm_warning = probe_cuda_sm_counts()
        self._refresh_handles()

    def _bind(self) -> None:
        nvml = self._nvml
        nvml.nvmlInit_v2.argtypes = []
        nvml.nvmlInit_v2.restype = ctypes.c_int
        nvml.nvmlShutdown.argtypes = []
        nvml.nvmlShutdown.restype = ctypes.c_int
        nvml.nvmlDeviceGetCount_v2.argtypes = [ctypes.POINTER(ctypes.c_uint)]
        nvml.nvmlDeviceGetCount_v2.restype = ctypes.c_int
        nvml.nvmlDeviceGetHandleByIndex_v2.argtypes = [
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        nvml.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
        nvml.nvmlDeviceGetName.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_char),
            ctypes.c_uint,
        ]
        nvml.nvmlDeviceGetName.restype = ctypes.c_int
        nvml.nvmlDeviceGetUUID.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_char),
            ctypes.c_uint,
        ]
        nvml.nvmlDeviceGetUUID.restype = ctypes.c_int
        nvml.nvmlDeviceGetMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_NvmlMemory),
        ]
        nvml.nvmlDeviceGetMemoryInfo.restype = ctypes.c_int
        nvml.nvmlDeviceGetUtilizationRates.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_NvmlUtilization),
        ]
        nvml.nvmlDeviceGetUtilizationRates.restype = ctypes.c_int
        for name in (
            "nvmlDeviceGetCurrPcieLinkGeneration",
            "nvmlDeviceGetCurrPcieLinkWidth",
        ):
            function = getattr(nvml, name)
            function.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
            function.restype = ctypes.c_int
        nvml.nvmlDeviceGetPcieThroughput.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_uint),
        ]
        nvml.nvmlDeviceGetPcieThroughput.restype = ctypes.c_int
        process_function = getattr(
            nvml,
            "nvmlDeviceGetComputeRunningProcesses_v3",
            getattr(nvml, "nvmlDeviceGetComputeRunningProcesses_v2", None),
        )
        if process_function is None:
            raise RuntimeError("NVML compute-process query is unavailable")
        process_function.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(_NvmlProcessInfo),
        ]
        process_function.restype = ctypes.c_int
        self._get_compute_processes = process_function
        nvml.nvmlDeviceGetProcessUtilization.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_NvmlProcessUtilizationSample),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.c_ulonglong,
        ]
        nvml.nvmlDeviceGetProcessUtilization.restype = ctypes.c_int

    def _refresh_handles(self) -> None:
        count = ctypes.c_uint()
        if self._nvml.nvmlDeviceGetCount_v2(ctypes.byref(count)) != self.NVML_SUCCESS:
            raise RuntimeError("NVML device count query failed")
        handles: list[ctypes.c_void_p] = []
        for index in range(count.value):
            handle = ctypes.c_void_p()
            if (
                self._nvml.nvmlDeviceGetHandleByIndex_v2(
                    index, ctypes.byref(handle)
                )
                == self.NVML_SUCCESS
            ):
                handles.append(handle)
        self._handles = handles

    def _string_value(self, function_name: str, handle: ctypes.c_void_p) -> str:
        buffer = ctypes.create_string_buffer(256)
        function = getattr(self._nvml, function_name)
        if function(handle, buffer, len(buffer)) != self.NVML_SUCCESS:
            return "unknown"
        return buffer.value.decode("utf-8", errors="replace")

    def _uint_value(self, function_name: str, handle: ctypes.c_void_p) -> int | None:
        value = ctypes.c_uint()
        if getattr(self._nvml, function_name)(handle, ctypes.byref(value)) != 0:
            return None
        return value.value

    def _refresh_inventory(self) -> None:
        inventory: list[dict[str, Any]] = []
        for index, handle in enumerate(self._handles):
            memory = _NvmlMemory()
            memory_status = self._nvml.nvmlDeviceGetMemoryInfo(
                handle, ctypes.byref(memory)
            )
            inventory.append(
                {
                    "gpu_index": index,
                    "name": self._string_value("nvmlDeviceGetName", handle),
                    "gpu_uuid": self._string_value("nvmlDeviceGetUUID", handle),
                    "memory_total_mib": (
                        memory.total / (1024.0 * 1024.0)
                        if memory_status == self.NVML_SUCCESS
                        else None
                    ),
                    "memory_used_mib": (
                        memory.used / (1024.0 * 1024.0)
                        if memory_status == self.NVML_SUCCESS
                        else None
                    ),
                    "pcie_gen": self._uint_value(
                        "nvmlDeviceGetCurrPcieLinkGeneration", handle
                    ),
                    "pcie_width": self._uint_value(
                        "nvmlDeviceGetCurrPcieLinkWidth", handle
                    ),
                    "sm_count": self._sm_counts.get(index),
                }
            )
        self._inventory = inventory
        self._inventory_updated = time.monotonic()

    def _process_memory(self, handle: ctypes.c_void_p) -> dict[int, float | None]:
        count = ctypes.c_uint()
        status = self._get_compute_processes(handle, ctypes.byref(count), None)
        if status == self.NVML_SUCCESS and count.value == 0:
            return {}
        if status != self.NVML_ERROR_INSUFFICIENT_SIZE or count.value == 0:
            return {}
        rows = (_NvmlProcessInfo * count.value)()
        status = self._get_compute_processes(handle, ctypes.byref(count), rows)
        if status != self.NVML_SUCCESS:
            return {}
        return {
            row.pid: (
                None
                if row.used_gpu_memory == self.NVML_VALUE_NOT_AVAILABLE
                else row.used_gpu_memory / (1024.0 * 1024.0)
            )
            for row in rows[: count.value]
        }

    def _process_utilization(self, index: int, handle: ctypes.c_void_p) -> None:
        count = ctypes.c_uint()
        last_timestamp = self._last_process_timestamps.get(index, 0)
        status = self._nvml.nvmlDeviceGetProcessUtilization(
            handle, None, ctypes.byref(count), last_timestamp
        )
        if status != self.NVML_ERROR_INSUFFICIENT_SIZE or count.value == 0:
            return
        rows = (_NvmlProcessUtilizationSample * count.value)()
        status = self._nvml.nvmlDeviceGetProcessUtilization(
            handle, rows, ctypes.byref(count), last_timestamp
        )
        if status != self.NVML_SUCCESS:
            return
        for row in rows[: count.value]:
            key = (index, row.pid)
            self._sm_by_process[key] = float(row.sm_util)
            self._sm_seen_monotonic[key] = time.monotonic()
            self._last_process_timestamps[index] = max(
                self._last_process_timestamps.get(index, 0), row.timestamp
            )

    def sample(
        self, targets: dict[str, set[int]]
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[int, dict[str, float | None]],
        list[str],
    ]:
        inventory_age = time.monotonic() - self._inventory_updated
        if inventory_age >= HARDWARE_REFRESH_INTERVAL_SECONDS:
            self._refresh_inventory()
        process_rows: list[dict[str, Any]] = []
        pcie: dict[int, dict[str, float | None]] = {}
        active_keys: set[tuple[int, int]] = set()
        target_pids = set().union(*targets.values()) if targets else set()
        now = time.monotonic()
        for index, handle in enumerate(self._handles):
            self._process_utilization(index, handle)
            process_memory = self._process_memory(handle)
            for pid, memory_mib in process_memory.items():
                key = (index, pid)
                active_keys.add(key)
                process_rows.append(
                    {
                        "pid": pid,
                        "gpu_index": index,
                        "gpu_uuid": (
                            self._inventory[index].get("gpu_uuid")
                            if index < len(self._inventory)
                            else None
                        ),
                        "gpu_memory_mib": memory_mib,
                        "sm_util_percent": (
                            self._sm_by_process.get(key, 0.0)
                            if now - self._sm_seen_monotonic.get(key, 0.0) <= 1.2
                            else 0.0
                        ),
                    }
                )
            utilization = _NvmlUtilization()
            utilization_status = self._nvml.nvmlDeviceGetUtilizationRates(
                handle, ctypes.byref(utilization)
            )
            rx_value, tx_value = ctypes.c_uint(), ctypes.c_uint()
            if target_pids.intersection(process_memory):
                rx_status = self._nvml.nvmlDeviceGetPcieThroughput(
                    handle, self.NVML_PCIE_UTIL_RX_BYTES, ctypes.byref(rx_value)
                )
                tx_status = self._nvml.nvmlDeviceGetPcieThroughput(
                    handle, self.NVML_PCIE_UTIL_TX_BYTES, ctypes.byref(tx_value)
                )
            else:
                rx_status = tx_status = self.NVML_ERROR_INSUFFICIENT_SIZE
            pcie[index] = {
                "gpu_util_percent": (
                    float(utilization.gpu)
                    if utilization_status == self.NVML_SUCCESS
                    else None
                ),
                "h2d_mb_s": (
                    rx_value.value / 1024.0
                    if rx_status == self.NVML_SUCCESS
                    else None
                ),
                "d2h_mb_s": (
                    tx_value.value / 1024.0
                    if tx_status == self.NVML_SUCCESS
                    else None
                ),
            }
        self._sm_by_process = {
            key: value
            for key, value in self._sm_by_process.items()
            if key in active_keys
        }
        self._sm_seen_monotonic = {
            key: value
            for key, value in self._sm_seen_monotonic.items()
            if key in active_keys
        }
        warnings = [self._sm_warning] if self._sm_warning else []
        return list(self._inventory), process_rows, pcie, warnings

    def close(self) -> None:
        if not self._closed:
            self._nvml.nvmlShutdown()
            self._closed = True


def parse_proc_meminfo(output: str) -> dict[str, float | None]:
    values: dict[str, int] = {}
    for line in output.splitlines():
        name, separator, remainder = line.partition(":")
        if not separator:
            continue
        try:
            values[name] = int(remainder.strip().split()[0])
        except (IndexError, ValueError):
            continue
    total_kib = values.get("MemTotal")
    available_kib = values.get("MemAvailable", values.get("MemFree"))
    used_kib = (
        max(0, total_kib - available_kib)
        if total_kib is not None and available_kib is not None
        else None
    )
    return {
        "total_mib": total_kib / 1024.0 if total_kib is not None else None,
        "available_mib": (
            available_kib / 1024.0 if available_kib is not None else None
        ),
        "used_mib": used_kib / 1024.0 if used_kib is not None else None,
    }


def parse_proc_cpu_total(output: str) -> tuple[int, int] | None:
    first_line = output.splitlines()[0].split() if output else []
    if not first_line or first_line[0] != "cpu":
        return None
    try:
        counters = [int(value) for value in first_line[1:]]
    except ValueError:
        return None
    if len(counters) < 4:
        return None
    idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
    return sum(counters), idle


def parse_proc_pid_ticks(output: str) -> int | None:
    """Return utime+stime from /proc/<pid>/stat, tolerating spaces in comm."""

    closing = output.rfind(")")
    if closing < 0:
        return None
    fields = output[closing + 1 :].split()
    try:
        return int(fields[11]) + int(fields[12])
    except (IndexError, ValueError):
        return None


def parse_proc_status_rss(output: str) -> float | None:
    for line in output.splitlines():
        if not line.startswith("VmRSS:"):
            continue
        try:
            return int(line.split()[1]) / 1024.0
        except (IndexError, ValueError):
            return None
    return None


def parse_cpu_hardware(output: str, allowed_cpus: set[int]) -> dict[str, Any]:
    model: str | None = None
    physical_cores: set[tuple[str, str]] = set()
    visible_processors: set[int] = set()
    for block in output.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, separator, value = line.partition(":")
            if separator:
                fields[key.strip()] = value.strip()
        try:
            processor = int(fields["processor"])
        except (KeyError, ValueError):
            continue
        if allowed_cpus and processor not in allowed_cpus:
            continue
        visible_processors.add(processor)
        model = model or fields.get("model name") or fields.get("hardware")
        if "physical id" in fields and "core id" in fields:
            physical_cores.add((fields["physical id"], fields["core id"]))
    logical_threads = len(visible_processors) or len(allowed_cpus) or os.cpu_count()
    return {
        "model": model,
        "physical_cores": len(physical_cores) or logical_threads,
        "logical_threads": logical_threads,
    }


class HostResourceSampler:
    """Stateful Linux host/process sampler used to compute CPU deltas."""

    def __init__(self) -> None:
        try:
            allowed_cpus = set(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            allowed_cpus = set(range(os.cpu_count() or 0))
        self.hardware = parse_cpu_hardware(
            read_text(pathlib.Path("/proc/cpuinfo")), allowed_cpus
        )
        self.gpu_sm_counts, self.gpu_sm_warning = probe_cuda_sm_counts()
        self._previous_cpu: tuple[int, int] | None = None
        self._previous_processes: dict[int, tuple[int, float]] = {}
        try:
            self._clock_ticks = os.sysconf("SC_CLK_TCK")
        except (ValueError, OSError):
            self._clock_ticks = 100

    def sample(self, targets: dict[str, set[int]]) -> dict[str, Any]:
        now = time.monotonic()
        cpu_total = parse_proc_cpu_total(read_text(pathlib.Path("/proc/stat")))
        cpu_percent: float | None = None
        if cpu_total is not None and self._previous_cpu is not None:
            total_delta = cpu_total[0] - self._previous_cpu[0]
            idle_delta = cpu_total[1] - self._previous_cpu[1]
            if total_delta > 0:
                cpu_percent = max(
                    0.0, min(100.0, 100.0 * (1 - idle_delta / total_delta))
                )
        self._previous_cpu = cpu_total

        process_rows: dict[str, dict[str, float | None]] = {}
        current_processes: dict[int, tuple[int, float]] = {}
        for role in RESOURCE_ROLES:
            cpu_sum = 0.0
            cpu_seen = False
            rss_sum = 0.0
            rss_seen = False
            for pid in targets.get(role, set()):
                ticks = parse_proc_pid_ticks(
                    read_text(pathlib.Path("/proc") / str(pid) / "stat")
                )
                if ticks is not None:
                    current_processes[pid] = (ticks, now)
                    previous = self._previous_processes.get(pid)
                    if previous is not None and now > previous[1]:
                        cpu_sum += (
                            (ticks - previous[0])
                            / self._clock_ticks
                            / (now - previous[1])
                            * 100.0
                        )
                        cpu_seen = True
                rss = parse_proc_status_rss(
                    read_text(pathlib.Path("/proc") / str(pid) / "status")
                )
                if rss is not None:
                    rss_sum += rss
                    rss_seen = True
            process_rows[role] = {
                "cpu_util_percent": max(0.0, cpu_sum) if cpu_seen else None,
                "ram_rss_mib": rss_sum if rss_seen else None,
            }
        self._previous_processes = current_processes
        memory = parse_proc_meminfo(read_text(pathlib.Path("/proc/meminfo")))
        return {
            "hardware": {**self.hardware, "ram_total_mib": memory["total_mib"]},
            "system": {
                "cpu_util_percent": cpu_percent,
                "logical_threads": self.hardware.get("logical_threads"),
                **memory,
            },
            "processes": process_rows,
        }


def build_resource_payload(
    targets: dict[str, set[int]],
    host: dict[str, Any],
    process_samples: list[dict[str, Any]],
    inventory: list[dict[str, Any]],
    pcie_samples: dict[int, dict[str, float | None]],
    *,
    backend: str,
    target_interval_seconds: float = RESOURCE_SAMPLE_INTERVAL_SECONDS,
    hardware_interval_seconds: float = HARDWARE_REFRESH_INTERVAL_SECONDS,
    errors: Sequence[str] = (),
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    """Combine one host sample with GPU samples from NVML or nvidia-smi."""

    inventory_rows = [dict(gpu) for gpu in inventory]
    for gpu in inventory_rows:
        gpu.update(
            pcie_samples.get(
                gpu["gpu_index"], {"h2d_mb_s": None, "d2h_mb_s": None}
            )
        )
    processes: dict[str, Any] = {}
    for role in RESOURCE_ROLES:
        pids = sorted(targets.get(role, set()))
        per_gpu: dict[Any, dict[str, Any]] = {}
        for item in process_samples:
            if item.get("pid") not in pids:
                continue
            key = item.get("gpu_index")
            gpu = per_gpu.setdefault(
                key,
                {
                    "gpu_index": key,
                    "gpu_uuid": item.get("gpu_uuid"),
                    "gpu_memory_mib": 0.0,
                    "sm_util_percent": 0.0,
                },
            )
            gpu["gpu_memory_mib"] += item.get("gpu_memory_mib") or 0.0
            gpu["sm_util_percent"] += item.get("sm_util_percent") or 0.0
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
            **host["processes"].get(role, {}),
            "gpus": gpu_rows,
        }

    target_pids = set().union(*targets.values())
    active_gpu_indexes = {
        item.get("gpu_index")
        for item in process_samples
        if item.get("pid") in target_pids
        and isinstance(item.get("gpu_index"), int)
    }
    device_gpu_values = [
        gpu.get("gpu_util_percent")
        for gpu in inventory_rows
        if gpu.get("gpu_index") in active_gpu_indexes
        and isinstance(gpu.get("gpu_util_percent"), (int, float))
    ]
    total_gpu_util = sum(device_gpu_values) if device_gpu_values else None
    tracked_gpu_util = sum(
        processes[role].get("sm_util_percent") or 0.0
        for role in ("sionna", "gpu_channel")
    )
    system = dict(host["system"])
    logical_threads = host["hardware"].get("logical_threads")
    host_cpu_percent = system.get("cpu_util_percent")
    busy_cpu_cores = (
        float(host_cpu_percent) * float(logical_threads) / 100.0
        if isinstance(host_cpu_percent, (int, float))
        and isinstance(logical_threads, int)
        and logical_threads > 0
        else None
    )
    tracked_cpu_cores = sum(
        (processes[role].get("cpu_util_percent") or 0.0) / 100.0
        for role in RESOURCE_ROLES
    )
    system["busy_cpu_cores"] = busy_cpu_cores
    system["other_os_cpu_cores"] = (
        max(0.0, busy_cpu_cores - tracked_cpu_cores)
        if busy_cpu_cores is not None
        else None
    )

    def pcie_total(key: str) -> float | None:
        values = [
            item[key]
            for item in pcie_samples.values()
            if item.get(key) is not None
        ]
        return sum(values) if values else None

    return {
        "sampled_unix_ms": time.time_ns() // 1_000_000,
        "available": not errors,
        "error": "; ".join(dict.fromkeys(errors)) if errors else None,
        "warning": "; ".join(dict.fromkeys(warnings)) if warnings else None,
        "sampling": {
            "backend": backend,
            "target_interval_ms": round(target_interval_seconds * 1000.0),
            "hardware_interval_ms": round(
                hardware_interval_seconds * 1000.0
            ),
        },
        "hardware": {"cpu": host["hardware"], "gpus": inventory_rows},
        "system": system,
        "gpu_compute": {
            "active_gpu_count": len(active_gpu_indexes),
            "total_sm_util_percent": total_gpu_util,
            "other_sm_util_percent": (
                max(0.0, total_gpu_util - tracked_gpu_util)
                if total_gpu_util is not None
                else None
            ),
        },
        "pcie": {
            "h2d_mb_s": pcie_total("h2d_mb_s"),
            "d2h_mb_s": pcie_total("d2h_mb_s"),
        },
        "processes": processes,
    }


def sample_nvml_gpu_usage(
    targets: dict[str, set[int]],
    host_sampler: HostResourceSampler,
    nvml_sampler: NvmlResourceSampler,
) -> dict[str, Any]:
    inventory, processes, pcie, warnings = nvml_sampler.sample(targets)
    return build_resource_payload(
        targets,
        host_sampler.sample(targets),
        processes,
        inventory,
        pcie,
        backend="nvml",
        warnings=warnings,
    )


def sample_gpu_usage(
    targets: dict[str, set[int]], host_sampler: HostResourceSampler | None = None
) -> dict[str, Any]:
    """Sample process GPU/host use plus device-wide hardware and PCIe traffic."""

    errors: list[str] = []
    warnings: list[str] = []
    sampler = host_sampler or HostResourceSampler()

    def run_query(arguments: list[str], *, required: bool = True) -> str:
        try:
            completed = subprocess.run(
                arguments,
                check=False,
                capture_output=True,
                text=True,
                timeout=4.0,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            (errors if required else warnings).append(str(exc))
            return ""
        if completed.returncode != 0:
            message = completed.stderr.strip() or f"{' '.join(arguments)} failed"
            (errors if required else warnings).append(message)
            return ""
        return completed.stdout

    pmon = parse_nvidia_pmon(
        run_query(["nvidia-smi", "pmon", "-c", "1", "-s", "um"])
    )
    inventory = parse_nvidia_gpu_inventory(
        run_query(
            [
                "nvidia-smi",
                "--query-gpu=index,name,uuid,memory.total,memory.used,pcie.link.gen.current,pcie.link.width.current,utilization.gpu",
                "--format=csv,noheader,nounits",
            ]
        )
    )
    sm_counts = getattr(sampler, "gpu_sm_counts", None)
    sm_probe_warning = getattr(sampler, "gpu_sm_warning", None)
    if sm_counts is None:
        sm_counts, sm_probe_warning = probe_cuda_sm_counts()
    if sm_probe_warning:
        warnings.append(sm_probe_warning)
    pcie_samples = parse_nvidia_dmon_pcie(
        run_query(
            ["nvidia-smi", "dmon", "-c", "1", "-s", "t"],
            required=False,
        )
    )
    for gpu in inventory:
        gpu["sm_count"] = sm_counts.get(gpu["gpu_index"])
    return build_resource_payload(
        targets,
        sampler.sample(targets),
        pmon,
        inventory,
        pcie_samples,
        backend="nvidia-smi-fallback",
        target_interval_seconds=0.5,
        hardware_interval_seconds=0.5,
        errors=errors,
        warnings=warnings,
    )


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
                "matrix_profile_active": observed.get("matrix_profile_active") is True,
                "array": observed.get("array") if isinstance(observed.get("array"), dict) else {},
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
        self._sionna_session_id: Any = None
        self._gpu_usage: dict[str, Any] | None = None
        self._gpu_history: list[dict[str, Any]] = []
        self._telemetry_history: list[dict[str, Any]] = []
        self._iteration_history: list[dict[str, Any]] = []
        self._warmup_targets: dict[tuple[Any, Any], dict[str, int]] = {}
        self._bad_telemetry_frames = 0

    def _iteration_event(
        self, session_id: Any, iteration: Any
    ) -> dict[str, Any] | None:
        for event in reversed(self._iteration_history):
            if (
                event.get("session_id") == session_id
                and event.get("iteration") == iteration
            ):
                return event
        return None

    def _close_completed_warmups(self, observed_unix_ms: int) -> None:
        completed: list[tuple[Any, Any]] = []
        for key, targets in self._warmup_targets.items():
            ready = True
            begin_times: dict[str, float] = {}
            end_times: dict[str, float] = {}
            begin_slots: dict[str, int] = {}
            end_slots: dict[str, int] = {}
            for link_id, expected_seqno in targets.items():
                payload = self._telemetry.get(link_id, {})
                slot = payload.get("slot")
                warmup_until_slot = payload.get("warmup_until_slot")
                exact_cycle = (
                    isinstance(payload.get("warmup_event_seq"), int)
                    and payload["warmup_event_seq"] > 0
                    and payload.get("warmup_profile_seqno") == expected_seqno
                )
                begin_ns = payload.get("warmup_begin_unix_ns")
                end_ns = payload.get("warmup_end_unix_ns")
                begin_slot = payload.get("warmup_begin_slot")
                end_slot = payload.get("warmup_end_slot")
                if exact_cycle and isinstance(begin_ns, int) and begin_ns > 0:
                    begin_times[link_id] = begin_ns / 1_000_000.0
                    if isinstance(begin_slot, int):
                        begin_slots[link_id] = begin_slot
                if exact_cycle and isinstance(end_ns, int) and end_ns > 0:
                    end_times[link_id] = end_ns / 1_000_000.0
                    if isinstance(end_slot, int):
                        end_slots[link_id] = end_slot
                if (
                    not isinstance(payload.get("seqno"), int)
                    or payload["seqno"] < expected_seqno
                    or payload.get("profile_active") is not True
                    or not isinstance(slot, int)
                    or (exact_cycle and link_id not in end_times)
                    or (
                        not exact_cycle
                        and isinstance(warmup_until_slot, int)
                        and warmup_until_slot > 0
                        and slot < warmup_until_slot
                    )
                ):
                    ready = False
                    break
            event = self._iteration_event(*key)
            if event is not None:
                if begin_slots:
                    event["warmup_begin_slots"] = begin_slots
                if end_slots:
                    event["warmup_end_slots"] = end_slots
                if len(begin_times) == len(targets):
                    event["warmup_started_unix_ms"] = min(begin_times.values())
            if not ready:
                continue
            if event is not None and event.get("warmup_ended_unix_ms") is None:
                if len(end_times) == len(targets):
                    event["warmup_ended_unix_ms"] = max(end_times.values())
                    event["warmup_boundary_source"] = "backend"
                else:
                    event["warmup_ended_unix_ms"] = observed_unix_ms
                    event["warmup_boundary_source"] = "telemetry_observed"
            completed.append(key)
        for key in completed:
            self._warmup_targets.pop(key, None)

    def update_telemetry(self, link_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            observed_unix_ms = time.time_ns() // 1_000_000
            self._telemetry[link_id] = payload
            self._telemetry_seen[link_id] = time.monotonic()
            slot_processing = payload.get("slot_processing")
            live = payload.get("live")
            self._telemetry_history.append(
                {
                    "observed_unix_ms": observed_unix_ms,
                    "link_id": link_id,
                    "slot": payload.get("slot"),
                    "backend": payload.get("backend"),
                    "slot_processing": (
                        dict(slot_processing)
                        if isinstance(slot_processing, dict)
                        else {}
                    ),
                    "live": dict(live) if isinstance(live, dict) else {},
                }
            )
            # The UI plots a five-second window. At 20 Hz and two live links,
            # 256 frames retain a little over six seconds without making each
            # /api/status response carry an unbounded telemetry log.
            del self._telemetry_history[:-256]
            self._close_completed_warmups(observed_unix_ms)

    def note_bad_telemetry(self) -> None:
        with self._lock:
            self._bad_telemetry_frames += 1

    def update_sionna(self, payload: dict[str, Any]) -> None:
        with self._lock:
            session_id = payload.get("session_id")
            if (
                session_id is not None
                and session_id != self._sionna_session_id
            ):
                if self._sionna_session_id is not None:
                    self._iteration_history.clear()
                    self._telemetry_history.clear()
                    self._warmup_targets.clear()
                    self._sionna = None
                    self._sionna_seen = None
                self._sionna_session_id = session_id
            if payload.get("event") == "sionna_rt_update":
                self._sionna = payload
                self._sionna_seen = time.monotonic()
                iteration = payload.get("iteration")
                received_unix_ms = time.time_ns() // 1_000_000
                started_unix_ms = payload.get("update_started_unix_ms")
                control_ack_unix_ms = payload.get("control_ack_unix_ms")
                timing = payload.get("timing_ms")
                total_update_ms = (
                    timing.get("total_update") if isinstance(timing, dict) else None
                )
                if not isinstance(started_unix_ms, int):
                    started_unix_ms = (
                        control_ack_unix_ms
                        if isinstance(control_ack_unix_ms, int)
                        else received_unix_ms
                    )
                if isinstance(total_update_ms, (int, float)):
                    ended_unix_ms = max(
                        started_unix_ms, round(started_unix_ms + total_update_ms)
                    )
                elif isinstance(control_ack_unix_ms, int):
                    ended_unix_ms = max(started_unix_ms, control_ack_unix_ms)
                else:
                    ended_unix_ms = max(started_unix_ms, received_unix_ms)
                reply = payload.get("control_reply")
                reply_links = reply.get("links", []) if isinstance(reply, dict) else []
                warmup_targets = {
                    item["link_id"]: item["seqno"]
                    for item in reply_links
                    if isinstance(item, dict)
                    and isinstance(item.get("link_id"), str)
                    and isinstance(item.get("seqno"), int)
                    and isinstance(item.get("warmup_until_slot"), int)
                    and item["warmup_until_slot"] > 0
                }
                event = self._iteration_event(session_id, iteration)
                if event is None:
                    event = {"iteration": iteration, "session_id": session_id}
                    self._iteration_history.append(event)
                event.update(
                    {
                        "observed_unix_ms": ended_unix_ms,
                        "started_unix_ms": started_unix_ms,
                        "ended_unix_ms": ended_unix_ms,
                        "warmup_expected": bool(warmup_targets),
                        "warmup_started_unix_ms": (
                            control_ack_unix_ms
                            if warmup_targets
                            and isinstance(control_ack_unix_ms, int)
                            else ended_unix_ms if warmup_targets else None
                        ),
                        "warmup_ended_unix_ms": None if warmup_targets else None,
                        "timing_ms": (
                            dict(timing) if isinstance(timing, dict) else {}
                        ),
                        "channels": [
                            {
                                key: channel.get(key)
                                for key in (
                                    "link_id",
                                    "direction",
                                    "total_path_power_db",
                                    "strongest_tap_gain_db",
                                    "ray_count",
                                    "tap_count",
                                )
                            }
                            for channel in payload.get("channels", [])
                            if isinstance(channel, dict)
                            and isinstance(channel.get("link_id"), str)
                        ],
                    }
                )
                key = (session_id, iteration)
                if warmup_targets:
                    self._warmup_targets[key] = warmup_targets
                    self._close_completed_warmups(received_unix_ms)
                else:
                    self._warmup_targets.pop(key, None)
                del self._iteration_history[:-512]
            elif payload.get("event") == "sionna_rt_runtime":
                self._sionna_runtime = payload
                self._sionna_runtime_seen = time.monotonic()
                if payload.get("phase") == "tracing_channels":
                    iteration = payload.get("iteration")
                    started_unix_ms = payload.get("observed_unix_ms")
                    if not isinstance(started_unix_ms, int):
                        started_unix_ms = time.time_ns() // 1_000_000
                    event = self._iteration_event(session_id, iteration)
                    if event is None:
                        self._iteration_history.append(
                            {
                                "observed_unix_ms": started_unix_ms,
                                "iteration": iteration,
                                "session_id": session_id,
                                "started_unix_ms": started_unix_ms,
                                "ended_unix_ms": None,
                                "warmup_expected": False,
                                "warmup_started_unix_ms": None,
                                "warmup_ended_unix_ms": None,
                            }
                        )
                        del self._iteration_history[:-512]

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
            "web_ui": {os.getpid()},
        }

    def update_gpu_usage(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._gpu_usage = payload
            roles: dict[str, Any] = {}
            raw_processes = payload.get("processes", {})
            for role in RESOURCE_ROLES:
                process = raw_processes.get(role, {})
                roles[role] = {
                    "running": process.get("running") is True,
                    "gpu_resident": process.get("gpu_resident") is True,
                    "sm_util_percent": process.get("sm_util_percent"),
                    "gpu_memory_mib": process.get("gpu_memory_mib"),
                    "cpu_util_percent": process.get("cpu_util_percent"),
                    "ram_rss_mib": process.get("ram_rss_mib"),
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
                    "system": dict(payload.get("system", {})),
                    "gpu_compute": dict(payload.get("gpu_compute", {})),
                    "pcie": dict(payload.get("pcie", {})),
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
            telemetry_history = list(self._telemetry_history)
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
            "history": {
                "telemetry": telemetry_history,
                "iterations": iteration_history[-32:],
            },
            "delivery": delivery_status(sionna, telemetry),
        }


class SionnaJsonlTail:
    def __init__(
        self,
        path: pathlib.Path,
        store: StatusStore,
        initial_tail_bytes: int = INITIAL_JSONL_TAIL_BYTES,
    ) -> None:
        self.path = path
        self.store = store
        self.initial_tail_bytes = max(1, initial_tail_bytes)
        self._offset: int | None = None

    def poll_once(self) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        initial_read = self._offset is None
        if initial_read:
            self._offset = max(0, size - self.initial_tail_bytes)
        elif size < self._offset:
            self._offset = 0
        with self.path.open("r", encoding="utf-8") as handle:
            handle.seek(self._offset)
            if initial_read and self._offset > 0:
                # The byte window will usually begin in the middle of one
                # large sionna_rt_update JSON object. Drop that fragment and
                # begin parsing at the next complete JSONL record.
                handle.readline()
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
    """Refresh host/GPU resource metrics without blocking HTTP polls."""

    host_sampler = HostResourceSampler()
    nvml_sampler: NvmlResourceSampler | None = None
    fallback_warning: str | None = None
    try:
        try:
            nvml_sampler = NvmlResourceSampler()
        except Exception as exc:
            fallback_warning = str(exc)

        next_sample = time.monotonic()
        while not stop.is_set():
            targets = store.process_targets()
            if nvml_sampler is not None:
                try:
                    payload = sample_nvml_gpu_usage(
                        targets, host_sampler, nvml_sampler
                    )
                except Exception as exc:
                    nvml_sampler.close()
                    nvml_sampler = None
                    fallback_warning = f"NVML sampling failed: {exc}"
                    payload = sample_gpu_usage(targets, host_sampler=host_sampler)
            else:
                payload = sample_gpu_usage(targets, host_sampler=host_sampler)
            if fallback_warning:
                warnings = [payload.get("warning"), fallback_warning]
                payload["warning"] = "; ".join(
                    dict.fromkeys(item for item in warnings if item)
                )
            store.update_gpu_usage(payload)

            interval = (
                RESOURCE_SAMPLE_INTERVAL_SECONDS
                if nvml_sampler is not None
                else 0.5
            )
            next_sample += interval
            next_sample = max(next_sample, time.monotonic())
            if stop.wait(max(0.0, next_sample - time.monotonic())):
                break
    finally:
        if nvml_sampler is not None:
            nvml_sampler.close()


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
