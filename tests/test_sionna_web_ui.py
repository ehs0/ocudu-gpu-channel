#!/usr/bin/env python3
"""Dependency-free tests for the read-only Sionna status web service."""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "web_ui"))

from server import (  # noqa: E402
    HARDWARE_REFRESH_INTERVAL_SECONDS,
    RESOURCE_SAMPLE_INTERVAL_SECONDS,
    SionnaJsonlTail,
    StatusStore,
    _NvmlProcessInfo,
    _NvmlProcessUtilizationSample,
    _NvmlUtilization,
    build_resource_payload,
    delivery_status,
    parse_cpu_hardware,
    parse_nvidia_compute_apps,
    parse_nvidia_dmon_pcie,
    parse_nvidia_gpu_inventory,
    parse_nvidia_gpu_map,
    parse_nvidia_pmon,
    parse_proc_cpu_total,
    parse_proc_meminfo,
    parse_proc_pid_ticks,
    parse_proc_status_rss,
    parse_telemetry_frame,
    sample_gpu_usage,
)


class WebUiTests(unittest.TestCase):
    def test_telemetry_topic_and_json_are_checked(self) -> None:
        frame = (
            'gnb0>ue0:sionna_rt '
            '{"event":"telemetry","link_id":"gnb0>ue0:sionna_rt","slot":17}'
        )
        topic, payload = parse_telemetry_frame(frame)
        self.assertEqual(topic, "gnb0>ue0:sionna_rt")
        self.assertEqual(payload["slot"], 17)
        with self.assertRaises(ValueError):
            parse_telemetry_frame(
                'gnb0>ue0:sionna_rt '
                '{"event":"telemetry","link_id":"gnb1>ue0:sionna_rt"}'
            )

    def test_store_exposes_read_only_feed_snapshot(self) -> None:
        store = StatusStore()
        store.update_telemetry(
            "ue0>gnb0:sionna_rt",
            {
                "event": "telemetry",
                "link_id": "ue0>gnb0:sionna_rt",
                "slot": 3,
            },
        )
        store.update_sionna(
            {
                "event": "sionna_rt_update",
                "iteration": 2,
                "positions": {},
                "scene_geometry": {
                    "objects": [{"id": "road", "kind": "road"}]
                },
            }
        )
        snapshot = store.snapshot()
        self.assertTrue(snapshot["feeds"]["telemetry_connected"])
        self.assertTrue(snapshot["feeds"]["sionna_connected"])
        self.assertEqual(snapshot["telemetry"]["ue0>gnb0:sionna_rt"]["slot"], 3)
        self.assertEqual(snapshot["sionna"]["iteration"], 2)
        self.assertEqual(
            snapshot["sionna"]["scene_geometry"]["objects"][0]["kind"],
            "road",
        )

    def test_store_exposes_runtime_phase_and_process_targets(self) -> None:
        store = StatusStore()
        store.update_sionna(
            {
                "event": "sionna_rt_runtime",
                "process_id": os.getpid(),
                "phase": "tracing_channels",
                "execution_mode": "single_process_repeating_update_loop",
            }
        )
        store.update_telemetry(
            "gnb0>ue0:sionna_rt",
            {
                "event": "telemetry",
                "link_id": "gnb0>ue0:sionna_rt",
                "process_id": 4321,
            },
        )
        snapshot = store.snapshot()
        self.assertTrue(snapshot["sionna_runtime"]["process_alive"])
        self.assertEqual(snapshot["sionna_runtime"]["phase"], "tracing_channels")
        self.assertEqual(store.process_targets()["sionna"], {os.getpid()})
        self.assertEqual(store.process_targets()["gpu_channel"], {4321})
        self.assertEqual(store.process_targets()["web_ui"], {os.getpid()})
        store.update_sionna(
            {
                "event": "sionna_rt_update",
                "session_id": "test-session",
                "iteration": 17,
                "control_ack_unix_ms": 123456,
            }
        )
        store.update_gpu_usage(
            {
                "sampled_unix_ms": 123500,
                "available": True,
                "processes": {
                    "sionna": {
                        "running": True,
                        "gpu_resident": True,
                        "sm_util_percent": 42.0,
                        "gpu_memory_mib": 1000.0,
                        "cpu_util_percent": 125.0,
                        "ram_rss_mib": 2048.0,
                    },
                    "gpu_channel": {
                        "running": True,
                        "gpu_resident": True,
                        "sm_util_percent": 18.0,
                        "gpu_memory_mib": 500.0,
                        "cpu_util_percent": 75.0,
                        "ram_rss_mib": 256.0,
                    },
                },
                "system": {
                    "cpu_util_percent": 33.0,
                    "used_mib": 8192.0,
                    "total_mib": 32768.0,
                },
                "pcie": {"h2d_mb_s": 1200.0, "d2h_mb_s": 300.0},
            }
        )
        history_snapshot = store.snapshot()["gpu_usage"]
        self.assertEqual(history_snapshot["history"][-1]["iteration"], 17)
        self.assertEqual(
            history_snapshot["history"][-1]["roles"]["sionna"]["sm_util_percent"],
            42.0,
        )
        self.assertEqual(
            history_snapshot["history"][-1]["roles"]["sionna"]["ram_rss_mib"],
            2048.0,
        )
        self.assertEqual(
            history_snapshot["history"][-1]["system"]["cpu_util_percent"], 33.0
        )
        self.assertEqual(
            history_snapshot["history"][-1]["pcie"]["h2d_mb_s"], 1200.0
        )
        self.assertEqual(history_snapshot["iterations"][-1]["iteration"], 17)

    def test_nvidia_process_samples_are_parsed_by_pid_and_gpu(self) -> None:
        gpu_map = parse_nvidia_gpu_map("0, GPU-aaaa\n1, GPU-bbbb\n")
        self.assertEqual(gpu_map, {"GPU-aaaa": 0, "GPU-bbbb": 1})
        apps = parse_nvidia_compute_apps(
            "GPU-bbbb, 1234, /usr/bin/python3, 812.5\n", gpu_map
        )
        self.assertEqual(apps[0]["gpu_index"], 1)
        self.assertEqual(apps[0]["gpu_memory_mib"], 812.5)
        pmon = parse_nvidia_pmon(
            "# gpu pid type sm mem fb command\n"
            "# Idx # C/G % % MB name\n"
            "1 1234 C 37 8 813 python3\n"
        )
        self.assertEqual(pmon[0]["pid"], 1234)
        self.assertEqual(pmon[0]["gpu_index"], 1)
        self.assertEqual(pmon[0]["sm_util_percent"], 37.0)
        self.assertEqual(pmon[0]["memory_util_percent"], 8.0)
        self.assertEqual(pmon[0]["gpu_memory_mib"], 813.0)

    def test_device_hardware_and_pcie_samples_are_parsed(self) -> None:
        inventory = parse_nvidia_gpu_inventory(
            "0, NVIDIA GeForce RTX 5090, GPU-aaaa, 32607, 1812, 5, 4, 62\n"
        )
        self.assertEqual(inventory[0]["name"], "NVIDIA GeForce RTX 5090")
        self.assertEqual(inventory[0]["memory_total_mib"], 32607.0)
        self.assertEqual(inventory[0]["pcie_gen"], 5)
        self.assertEqual(inventory[0]["gpu_util_percent"], 62.0)
        pcie = parse_nvidia_dmon_pcie(
            "# gpu rxpci txpci\n"
            "# Idx MB/s MB/s\n"
            "0 12288 1400\n"
        )
        self.assertEqual(pcie[0]["h2d_mb_s"], 12288.0)
        self.assertEqual(pcie[0]["d2h_mb_s"], 1400.0)

    def test_linux_cpu_and_memory_samples_are_parsed(self) -> None:
        memory = parse_proc_meminfo(
            "MemTotal:       65536 kB\n"
            "MemFree:         4096 kB\n"
            "MemAvailable:   16384 kB\n"
        )
        self.assertEqual(memory["total_mib"], 64.0)
        self.assertEqual(memory["available_mib"], 16.0)
        self.assertEqual(memory["used_mib"], 48.0)
        self.assertEqual(parse_proc_cpu_total("cpu  10 2 3 40 5 0 0 0\n"), (60, 45))
        self.assertEqual(parse_proc_status_rss("Name:\ttest\nVmRSS:\t2048 kB\n"), 2.0)
        pid_stat = "123 (worker with spaces) R 1 2 3 4 5 6 7 8 9 10 120 30 0 0"
        self.assertEqual(parse_proc_pid_ticks(pid_stat), 150)

        cpu_info = parse_cpu_hardware(
            "processor: 0\nphysical id: 0\ncore id: 0\nmodel name: Test CPU\n\n"
            "processor: 1\nphysical id: 0\ncore id: 0\nmodel name: Test CPU\n\n"
            "processor: 2\nphysical id: 0\ncore id: 1\nmodel name: Test CPU\n",
            {0, 1, 2},
        )
        self.assertEqual(cpu_info["model"], "Test CPU")
        self.assertEqual(cpu_info["physical_cores"], 2)
        self.assertEqual(cpu_info["logical_threads"], 3)

    def test_web_ui_uses_resource_graphs_without_mem_utilization_card(self) -> None:
        index = (PROJECT_ROOT / "scripts" / "web_ui" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("현재 MEM", index)
        self.assertIn('id="gpuComputeChart"', index)
        self.assertIn('id="applicationCpuChart"', index)
        self.assertIn('id="memoryChart"', index)
        self.assertIn('id="pcieChart"', index)
        self.assertNotIn('id="computeChart"', index)
        self.assertIn("GPU Compute (%)", index)
        self.assertIn("Application CPU (논리 코어)", index)
        self.assertIn("label:'전체 GPU'", index)
        self.assertIn("label:'기타 GPU'", index)
        self.assertIn("label:'Web UI'", index)
        self.assertIn("label:'기타/OS'", index)
        self.assertNotIn("label:'Host CPU'", index)
        self.assertIn("windowMs=5000", index)
        self.assertIn("secondTicks=[5,4,3,2,1,0]", index)
        self.assertIn("`${secondsAgo}초 전`", index)
        self.assertIn("hardware-card.active", index)
        self.assertIn("started_unix_ms", index)
        self.assertIn("warmup_started_unix_ms", index)
        self.assertIn("maxGapMs", index)
        self.assertIn("warmupMarkerWidth=6", index)
        self.assertIn("W warm-up 강조", index)
        self.assertNotIn("ctx.fillRect(x0,top,Math.max(1,x1-x0),plotH)", index)

    def test_store_tracks_iteration_boundaries_and_warmup_interval(self) -> None:
        store = StatusStore()
        store.update_sionna(
            {
                "event": "sionna_rt_runtime",
                "session_id": "timeline-session",
                "iteration": 4,
                "phase": "tracing_channels",
                "observed_unix_ms": 1_000,
            }
        )
        running_event = store.snapshot()["gpu_usage"]
        self.assertIsNone(running_event)

        store.update_sionna(
            {
                "event": "sionna_rt_update",
                "session_id": "timeline-session",
                "iteration": 4,
                "update_started_unix_ms": 1_000,
                "control_ack_unix_ms": 1_200,
                "timing_ms": {"total_update": 250.0},
                "control_reply": {
                    "links": [
                        {
                            "link_id": "gnb0>ue0:sionna_rt",
                            "seqno": 7,
                            "warmup_until_slot": 12,
                        }
                    ]
                },
            }
        )
        store.update_gpu_usage(
            {"sampled_unix_ms": 1_260, "processes": {}, "system": {}, "pcie": {}}
        )
        event = store.snapshot()["gpu_usage"]["iterations"][-1]
        self.assertEqual(event["started_unix_ms"], 1_000)
        self.assertEqual(event["ended_unix_ms"], 1_250)
        self.assertEqual(event["warmup_started_unix_ms"], 1_200)
        self.assertIsNone(event["warmup_ended_unix_ms"])

        with mock.patch("server.time.time_ns", return_value=1_300_000_000):
            store.update_telemetry(
                "gnb0>ue0:sionna_rt",
                {
                    "event": "telemetry",
                    "link_id": "gnb0>ue0:sionna_rt",
                    "seqno": 7,
                    "slot": 12,
                    "profile_active": True,
                    "warmup_until_slot": 0,
                    "warmup_event_seq": 4,
                    "warmup_profile_seqno": 7,
                    "warmup_begin_slot": 11,
                    "warmup_end_slot": 12,
                    "warmup_begin_unix_ns": 1_205_000_000,
                    "warmup_end_unix_ns": 1_213_000_000,
                },
            )
        event = store.snapshot()["gpu_usage"]["iterations"][-1]
        self.assertEqual(event["warmup_started_unix_ms"], 1_205.0)
        self.assertEqual(event["warmup_ended_unix_ms"], 1_213.0)
        self.assertEqual(event["warmup_begin_slots"], {"gnb0>ue0:sionna_rt": 11})
        self.assertEqual(event["warmup_end_slots"], {"gnb0>ue0:sionna_rt": 12})
        self.assertEqual(event["warmup_boundary_source"], "backend")

    def test_store_keeps_observation_fallback_for_legacy_telemetry(self) -> None:
        store = StatusStore()
        store.update_sionna(
            {
                "event": "sionna_rt_update",
                "session_id": "legacy-session",
                "iteration": 1,
                "control_ack_unix_ms": 2_000,
                "control_reply": {
                    "links": [
                        {
                            "link_id": "legacy-link",
                            "seqno": 3,
                            "warmup_until_slot": 8,
                        }
                    ]
                },
            }
        )
        store.update_gpu_usage(
            {"sampled_unix_ms": 2_010, "processes": {}, "system": {}, "pcie": {}}
        )
        with mock.patch("server.time.time_ns", return_value=2_050_000_000):
            store.update_telemetry(
                "legacy-link",
                {
                    "event": "telemetry",
                    "link_id": "legacy-link",
                    "seqno": 3,
                    "slot": 8,
                    "profile_active": True,
                    "warmup_until_slot": 0,
                },
            )
        event = store.snapshot()["gpu_usage"]["iterations"][-1]
        self.assertEqual(event["warmup_started_unix_ms"], 2_000)
        self.assertEqual(event["warmup_ended_unix_ms"], 2_050)
        self.assertEqual(event["warmup_boundary_source"], "telemetry_observed")

    def test_resource_sample_combines_gpu_host_and_pcie_metrics(self) -> None:
        class FakeHostSampler:
            def sample(self, _targets: object) -> dict[str, object]:
                return {
                    "hardware": {
                        "model": "Test CPU",
                        "physical_cores": 8,
                        "logical_threads": 16,
                        "ram_total_mib": 32768.0,
                    },
                    "system": {
                        "cpu_util_percent": 25.0,
                        "total_mib": 32768.0,
                        "available_mib": 24576.0,
                        "used_mib": 8192.0,
                    },
                    "processes": {
                        "sionna": {
                            "cpu_util_percent": 110.0,
                            "ram_rss_mib": 2048.0,
                        },
                        "gpu_channel": {
                            "cpu_util_percent": 45.0,
                            "ram_rss_mib": 256.0,
                        },
                    },
                }

        def fake_run(arguments: list[str], **_kwargs: object) -> mock.Mock:
            command = " ".join(arguments)
            if " pmon " in f" {command} ":
                output = (
                    "# gpu pid type sm mem fb command\n"
                    "# Idx # C/G % % MB name\n"
                    "0 1234 C 37 8 813 python3\n"
                    "0 4321 C 12 1 512 channel\n"
                )
            elif " dmon " in f" {command} ":
                output = "# gpu rxpci txpci\n# Idx MB/s MB/s\n0 12000 1500\n"
            else:
                output = "0, Test GPU, GPU-aaaa, 32607, 2048, 5, 4\n"
            return mock.Mock(returncode=0, stdout=output, stderr="")

        with mock.patch("server.subprocess.run", side_effect=fake_run), mock.patch(
            "server.probe_cuda_sm_counts", return_value=({0: 170}, None)
        ), mock.patch("server.process_is_alive", return_value=True):
            payload = sample_gpu_usage(
                {"sionna": {1234}, "gpu_channel": {4321}},
                host_sampler=FakeHostSampler(),  # type: ignore[arg-type]
            )
        self.assertTrue(payload["available"])
        self.assertEqual(payload["hardware"]["gpus"][0]["sm_count"], 170)
        self.assertEqual(payload["pcie"]["h2d_mb_s"], 12000.0)
        self.assertEqual(payload["processes"]["sionna"]["cpu_util_percent"], 110.0)
        self.assertEqual(payload["processes"]["sionna"]["ram_rss_mib"], 2048.0)
        self.assertNotIn("memory_util_percent", payload["processes"]["sionna"])
        self.assertEqual(payload["sampling"]["backend"], "nvidia-smi-fallback")

    def test_fast_resource_payload_uses_nvml_cadence_and_layout(self) -> None:
        host = {
            "hardware": {
                "model": "Test CPU",
                "physical_cores": 8,
                "logical_threads": 16,
                "ram_total_mib": 32768.0,
            },
            "system": {"cpu_util_percent": 20.0},
            "processes": {
                "sionna": {"cpu_util_percent": 25.0, "ram_rss_mib": 1024.0},
                "gpu_channel": {
                    "cpu_util_percent": 50.0,
                    "ram_rss_mib": 256.0,
                },
                "web_ui": {"cpu_util_percent": 10.0, "ram_rss_mib": 128.0},
            },
        }
        payload = build_resource_payload(
            {"sionna": {1234}, "gpu_channel": {4321}, "web_ui": {9999}},
            host,
            [
                {
                    "pid": 1234,
                    "gpu_index": 0,
                    "gpu_uuid": "GPU-aaaa",
                    "gpu_memory_mib": 1800.0,
                    "sm_util_percent": 35.0,
                },
                {
                    "pid": 4321,
                    "gpu_index": 0,
                    "gpu_uuid": "GPU-aaaa",
                    "gpu_memory_mib": 500.0,
                    "sm_util_percent": 15.0,
                },
            ],
            [
                {
                    "gpu_index": 0,
                    "gpu_uuid": "GPU-aaaa",
                    "name": "Test GPU",
                    "memory_total_mib": 32000.0,
                    "memory_used_mib": 2300.0,
                }
            ],
            {
                0: {
                    "gpu_util_percent": 60.0,
                    "h2d_mb_s": 2200.0,
                    "d2h_mb_s": 800.0,
                }
            },
            backend="nvml",
        )
        self.assertEqual(RESOURCE_SAMPLE_INTERVAL_SECONDS, 0.1)
        self.assertEqual(HARDWARE_REFRESH_INTERVAL_SECONDS, 1.0)
        self.assertEqual(payload["sampling"]["backend"], "nvml")
        self.assertEqual(payload["sampling"]["target_interval_ms"], 100)
        self.assertEqual(payload["sampling"]["hardware_interval_ms"], 1000)
        self.assertEqual(payload["processes"]["sionna"]["sm_util_percent"], 35.0)
        self.assertEqual(payload["processes"]["web_ui"]["cpu_util_percent"], 10.0)
        self.assertEqual(payload["gpu_compute"]["total_sm_util_percent"], 60.0)
        self.assertEqual(payload["gpu_compute"]["other_sm_util_percent"], 10.0)
        self.assertAlmostEqual(payload["system"]["busy_cpu_cores"], 3.2)
        self.assertAlmostEqual(payload["system"]["other_os_cpu_cores"], 2.35)
        self.assertEqual(payload["pcie"]["h2d_mb_s"], 2200.0)
        self.assertEqual(_NvmlUtilization.memory.offset, 4)
        self.assertEqual(_NvmlProcessInfo.used_gpu_memory.offset, 8)
        self.assertEqual(_NvmlProcessUtilizationSample.timestamp.offset, 8)

    def test_jsonl_tail_ignores_non_sionna_and_malformed_records(self) -> None:
        store = StatusStore()
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "status.jsonl"
            path.write_text(
                "not-json\n"
                + json.dumps({"event": "something_else"})
                + "\n"
                + json.dumps({"event": "sionna_rt_update", "iteration": 9})
                + "\n",
                encoding="utf-8",
            )
            SionnaJsonlTail(path, store).poll_once()
        self.assertEqual(store.snapshot()["sionna"]["iteration"], 9)

    def test_jsonl_tail_starts_from_recent_window_of_large_file(self) -> None:
        store = StatusStore()
        old = json.dumps(
            {
                "event": "sionna_rt_update",
                "session_id": "old-session",
                "iteration": 999,
                "padding": "x" * 1024,
            }
        ) + "\n"
        recent = (
            json.dumps(
                {
                    "event": "sionna_rt_runtime",
                    "session_id": "current-session",
                    "process_id": os.getpid(),
                    "phase": "tracing_channels",
                    "iteration": 4,
                    "observed_unix_ms": 4_000,
                }
            )
            + "\n"
            + json.dumps(
                {
                    "event": "sionna_rt_update",
                    "session_id": "current-session",
                    "iteration": 4,
                    "update_started_unix_ms": 4_000,
                    "control_ack_unix_ms": 4_050,
                }
            )
            + "\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "large-status.jsonl"
            path.write_text(old + recent, encoding="utf-8")
            SionnaJsonlTail(
                path, store, initial_tail_bytes=len(recent.encode("utf-8")) + 20
            ).poll_once()
        store.update_gpu_usage(
            {"sampled_unix_ms": 4_060, "processes": {}, "system": {}, "pcie": {}}
        )
        snapshot = store.snapshot()
        self.assertEqual(snapshot["sionna"]["session_id"], "current-session")
        self.assertEqual(snapshot["sionna"]["iteration"], 4)
        self.assertEqual(
            {item["session_id"] for item in snapshot["gpu_usage"]["iterations"]},
            {"current-session"},
        )

    def test_new_sionna_session_clears_previous_timeline(self) -> None:
        store = StatusStore()
        for session_id, iteration, observed in (
            ("old-session", 99, 1_000),
            ("new-session", 1, 2_000),
        ):
            store.update_sionna(
                {
                    "event": "sionna_rt_runtime",
                    "session_id": session_id,
                    "process_id": os.getpid(),
                    "phase": "tracing_channels",
                    "iteration": iteration,
                    "observed_unix_ms": observed,
                }
            )
        store.update_gpu_usage(
            {"sampled_unix_ms": 2_010, "processes": {}, "system": {}, "pcie": {}}
        )
        iterations = store.snapshot()["gpu_usage"]["iterations"]
        self.assertEqual(len(iterations), 1)
        self.assertEqual(iterations[0]["session_id"], "new-session")
        self.assertEqual(iterations[0]["iteration"], 1)

    def test_delivery_requires_matching_ack_and_backend_seqnos(self) -> None:
        sionna = {
            "event": "sionna_rt_update",
            "batch_id": "sionna-4",
            "channels": [{"link_id": "gnb0>ue0:sionna_rt"}],
            "control_reply": {
                "ok": True,
                "batch_id": "sionna-4",
                "backend": "cuda",
                "link_count": 1,
                "links": [
                    {"link_id": "gnb0>ue0:sionna_rt", "seqno": 12}
                ],
            },
        }
        waiting = delivery_status(
            sionna,
            {
                "gnb0>ue0:sionna_rt": {
                    "seqno": 11,
                    "slot": 90,
                    "backend": "cuda",
                    "profile_active": True,
                }
            },
        )
        self.assertTrue(waiting["control_received"])
        self.assertFalse(waiting["backend_applied"])
        self.assertEqual(waiting["state"], "awaiting_backend_telemetry")

        confirmed = delivery_status(
            sionna,
            {
                "gnb0>ue0:sionna_rt": {
                    "seqno": 12,
                    "slot": 91,
                    "backend": "cuda",
                    "profile_active": True,
                }
            },
        )
        self.assertTrue(confirmed["backend_applied"])
        self.assertEqual(confirmed["backend_applied_links"], 1)
        self.assertEqual(confirmed["links"][0]["observed_slot"], 91)

    def test_delivery_does_not_call_dry_run_received(self) -> None:
        status = delivery_status(
            {
                "event": "sionna_rt_update",
                "batch_id": "sionna-0",
                "control_reply": {"ok": True, "dry_run": True},
            },
            {},
        )
        self.assertEqual(status["state"], "dry_run")
        self.assertFalse(status["control_received"])

    def test_delivery_distinguishes_applied_warmup_from_usable(self) -> None:
        sionna = {
            "event": "sionna_rt_update",
            "batch_id": "sionna-warmup",
            "channels": [{"link_id": "gnb0>ue0:sionna_rt"}],
            "control_reply": {
                "ok": True,
                "batch_id": "sionna-warmup",
                "backend": "cuda",
                "link_count": 1,
                "links": [
                    {
                        "link_id": "gnb0>ue0:sionna_rt",
                        "seqno": 13,
                        "warmup_until_slot": 92,
                    }
                ],
            },
        }
        warming = delivery_status(
            sionna,
            {
                "gnb0>ue0:sionna_rt": {
                    "seqno": 13,
                    "slot": 91,
                    "backend": "cuda",
                    "profile_active": True,
                    "warmup_until_slot": 92,
                }
            },
        )
        self.assertTrue(warming["backend_applied"])
        self.assertFalse(warming["backend_usable"])
        self.assertEqual(warming["state"], "backend_warmup")
        self.assertEqual(warming["warmup_links"], 1)
        self.assertTrue(warming["links"][0]["warming_up"])

        usable = delivery_status(
            sionna,
            {
                "gnb0>ue0:sionna_rt": {
                    "seqno": 13,
                    "slot": 92,
                    "backend": "cuda",
                    "profile_active": True,
                    "warmup_until_slot": 0,
                }
            },
        )
        self.assertTrue(usable["backend_usable"])
        self.assertEqual(usable["state"], "backend_applied")


if __name__ == "__main__":
    unittest.main()
