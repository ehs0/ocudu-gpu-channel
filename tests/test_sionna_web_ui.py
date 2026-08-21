#!/usr/bin/env python3
"""Dependency-free tests for the read-only Sionna status web service."""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "web_ui"))

from server import (  # noqa: E402
    SionnaJsonlTail,
    StatusStore,
    delivery_status,
    parse_nvidia_compute_apps,
    parse_nvidia_gpu_map,
    parse_nvidia_pmon,
    parse_telemetry_frame,
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
                        "memory_util_percent": 7.0,
                        "gpu_memory_mib": 1000.0,
                    },
                    "gpu_channel": {
                        "running": True,
                        "gpu_resident": True,
                        "sm_util_percent": 18.0,
                        "memory_util_percent": 2.0,
                        "gpu_memory_mib": 500.0,
                    },
                },
            }
        )
        history_snapshot = store.snapshot()["gpu_usage"]
        self.assertEqual(history_snapshot["history"][-1]["iteration"], 17)
        self.assertEqual(
            history_snapshot["history"][-1]["roles"]["sionna"]["sm_util_percent"],
            42.0,
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
