"""Single-cell scheduler benchmark: scaling, frozen traces, audit gates, lifecycle.

Runs without Sionna, a GPU or the radio stack. The bundles here are written
by the same serializers the live build uses (channel_trace.record_for,
traffic_trace.build) with synthetic channel profiles; the broker's half of the
contract (sample-exact application, noise epoch) is tests/test_sample_indexed_control.cpp,
and the live end-to-end runs are recorded in scene_validation.json.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
BENCH = PROJECT_ROOT / "examples" / "scheduler_benchmark"
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "sionna_rt"))
sys.path.insert(0, str(BENCH))

import channel_trace  # noqa: E402
import controller  # noqa: E402
import definitions as bench  # noqa: E402
import make_scenario  # noqa: E402
import scene_registry  # noqa: E402
import single_cell_runner  # noqa: E402
import traffic_trace  # noqa: E402
import validation  # noqa: E402
from channel_adapter import LaneProfile, MatrixProfile, Tap, make_matrix_profile_swap  # noqa: E402

PERIOD = 2_304_000


def fake_channel_bundle(root: pathlib.Path, ue_count: int = 2, updates: int = 20, noise: bool = True) -> pathlib.Path:
    """A channel bundle with the live build's record format and manifest."""

    bundle = root / "channel"
    bundle.mkdir(parents=True)
    links = [f"gnb0>ue{i}:sionna_rt" for i in range(ue_count)] + [f"ue{i}>gnb0:sionna_rt" for i in range(ue_count)]
    digests = []
    with (bundle / channel_trace.TRACE_FILE).open("w") as handle:
        for index in range(updates + 1):
            k = None if index == 0 else index - 1
            profiles = {link: MatrixProfile(1, 1, (LaneProfile(0, 0, (Tap(0.0, -10.0 - 0.01 * index - n, 0.1 * n),)),))
                        for n, link in enumerate(links)}
            record = channel_trace.record_for("attach" if k is None else "update", k, 0.0 if k is None else k / 10,
                                              {"gnb0": (0, 0, 8)}, profiles, make_matrix_profile_swap)
            digests.append(record["batch_sha256"])
            handle.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
    config = {"update_hz": 10.0, "updates": updates, "sample_rate_hz": 23_040_000.0, "samples_per_update": PERIOD,
              "noise": {"enabled": noise}}
    manifest = {"format": channel_trace.FORMAT, "trace_file": channel_trace.TRACE_FILE,
                "trace_sha256": channel_trace.file_sha256(bundle / channel_trace.TRACE_FILE), "records": updates + 1,
                "attach_batch_sha256": digests[0], "update_batch_sha256": digests[1:], "link_ids": sorted(links),
                "config": config, "noise_sha256": "n", "scenario_sha256": "s"}
    (bundle / channel_trace.MANIFEST_FILE).write_text(json.dumps(manifest))
    return bundle


def fake_run(run_id: str, arm: str, bundle: pathlib.Path, traffic: pathlib.Path, s0: int, ue_count: int = 2,
             digest_salt: str = "") -> dict:
    """A run whose evidence is exactly what a perfect live run records."""

    manifest = json.loads((bundle / channel_trace.MANIFEST_FILE).read_text())
    updates = manifest["config"]["updates"]
    records = list(channel_trace.iter_records(bundle))
    applied = {}
    for link in manifest["link_ids"]:
        def digest(k):
            return hashlib.sha256(f"{link}:{k}{digest_salt if k == 3 else ''}".encode()).hexdigest()[:16]
        applied[link] = {
            "attach": {"sha16": records[0]["link_sha256"][link][:16], "digest": digest("attach")},
            "updates": updates, "missing": [], "missing_count": 0, "exact": updates, "relative_ok": updates,
            "monotonic": True, "off_target": [],
            "digests": {str(k): digest(k) for k in range(updates)},
            "sha16": {str(k): records[k + 1]["link_sha256"][link][:16] for k in range(updates)},
        }
    epochs = {node: {"sample": s0, "applied": 1} for node in ["gnb0"] + [f"ue{i}" for i in range(ue_count)]}
    report = {
        "epoch": {"s0": s0, "trace_sha256": manifest["trace_sha256"]}, "ue_count": ue_count,
        "measure_seconds": 2, "segments_complete": 2, "segments_valid": 2, "runtime": {"real_time_ratio": 0.5},
        "evidence": {"applied": applied, "duplicate_applies": 0, "malformed_evidence_lines": 0, "noise_epochs": epochs,
                     "noise_missed": [],
                     "traffic": {"expected_packets": 10, "sent_packets": 10, "offered_relative_sha256": "same",
                                 "lag_samples": {"p99": 30000, "max": 60000}}},
        "segments": [], "metrics": None,
    }
    test = {"scheduler": "rr" if arm == "test1" else "qos"}
    return {"run_id": run_id, "arm": arm, "test": test, "label": bench.test_label(arm, test), "report": report,
            "gate": {"status": "passed", "all_ues_attached": True, "broker_clean": True, "measure_status": "completed"},
            "parameters": {"channel_bundle": str(bundle), "traffic_bundle": str(traffic)}}


class Definitions(unittest.TestCase):
    def test_ue_records_extend_the_multi_ue_table(self) -> None:
        sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "native"))
        import importlib.util
        spec = importlib.util.spec_from_file_location("mue", PROJECT_ROOT / "scripts/native/render-multi-ue-configs.py")
        mue = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mue)
        for index, record in enumerate(mue.UES):
            self.assertEqual(bench.single_cell_ue(index), record)
        self.assertEqual(bench.MAX_SINGLE_CELL_UES, 16)
        ids = [bench.single_cell_ue(i) for i in range(bench.MAX_SINGLE_CELL_UES)]
        for key in ("imsi", "imei", "ipv4", "netns", "tx_port", "rx_port"):
            self.assertEqual(len({r[key] for r in ids}), len(ids), key)

    def test_profiles_cover_every_ue(self) -> None:
        for n in (1, 8, 16):
            mixed = bench.single_cell_profile("mixed", n)
            self.assertEqual([u["role"] for u in mixed["ues"]], ["bulk" if i % 2 == 0 else "interactive" for i in range(n)])
            full = bench.single_cell_profile("full_buffer", n)
            self.assertAlmostEqual(full["offered_mbps"]["dl"], bench.FULL_BUFFER_TOTAL_DL_MBPS)
            self.assertTrue(all(u["five_qi"] == 9 for u in full["ues"]))
        with self.assertRaises(ValueError):
            bench.single_cell_profile("mixed", 17)


class ScenarioScaling(unittest.TestCase):
    SCENE = "sutd_1gnb"

    def test_only_one_gnb_scenes_are_offered(self) -> None:
        self.assertEqual(list(scene_registry.SCENES), [self.SCENE, "simple_blocks_2x2"])
        for catalog in scene_registry.catalog(True)["scenes"]:
            self.assertTrue(catalog["installed"], catalog["missing_files"])
            self.assertEqual(catalog["ue_counts"], [1, 2, 4])
            self.assertEqual(catalog["mobility_modes"], ["static", "pedestrian"])
            self.assertEqual(catalog["placement_modes"], ["uniform", "hotspot"])
            base = scene_registry.base_scenario(catalog["id"])
            self.assertEqual([n for n in base["nodes"] if n.startswith("gnb")], ["gnb0"])

    def test_simple_blocks_materials_and_heights_differ(self) -> None:
        manifest = json.loads(scene_registry.scene_files("simple_blocks_2x2")["manifest"].read_text())
        buildings = manifest["buildings"]
        self.assertEqual(len(buildings), 4)
        self.assertEqual({b["material"] for b in buildings}, {"concrete", "glass", "wood"})
        self.assertEqual(len({b["height_m"] for b in buildings}), 4)

    def test_adding_ues_keeps_existing_ues(self) -> None:
        for mobility in scene_registry.MOBILITY_MODES:
            for placement in scene_registry.PLACEMENT_MODES:
                one = make_scenario.generate_single_cell(3, self.SCENE, 1, mobility=mobility, placement=placement,
                                                         duration_s=40)
                many = make_scenario.generate_single_cell(3, self.SCENE, 16, mobility=mobility, placement=placement,
                                                          duration_s=40)
                self.assertEqual(one["nodes"]["ue0"], many["nodes"]["ue0"], (mobility, placement))
                self.assertEqual(len(many["links"]), 32)
        again = make_scenario.generate_single_cell(3, self.SCENE, 16, mobility="pedestrian", placement="hotspot",
                                                   duration_s=40)
        self.assertEqual(json.dumps(many, sort_keys=True), json.dumps(again, sort_keys=True))

    def test_modes_place_and_move_as_declared(self) -> None:
        region = scene_registry.region(self.SCENE)
        for mobility in scene_registry.MOBILITY_MODES:
            for placement in scene_registry.PLACEMENT_MODES:
                scenario = make_scenario.generate_single_cell(5, self.SCENE, 8, mobility=mobility, placement=placement,
                                                              duration_s=40)
                meta = scenario["benchmark"]
                for index in range(8):
                    node = scenario["nodes"][f"ue{index}"]
                    points = node.get("route_m") or [node["start_m"]]
                    for point in points:
                        self.assertTrue(region.allows(point), (mobility, placement, index, point))
                    if mobility == "static":
                        self.assertNotIn("route_m", node)
                    else:
                        self.assertEqual(node["speed_mps"], scene_registry.PEDESTRIAN_SPEED_MPS)
                    if placement == "hotspot":
                        centre = meta["hotspots"]["centres_m"][index % scene_registry.HOTSPOT_COUNT]
                        # Gaussian sigma 8 m per axis: every point within 5 sigma of its centre.
                        for point in points:
                            self.assertLess(((point[0] - centre[0]) ** 2 + (point[1] - centre[1]) ** 2) ** 0.5,
                                            5 * scene_registry.HOTSPOT_SIGMA_M)
                if placement == "hotspot":
                    self.assertEqual(len(meta["hotspots"]["centres_m"]), scene_registry.HOTSPOT_COUNT)
                else:
                    self.assertIsNone(meta["hotspots"])

    def test_hotspot_clusters_are_tighter_than_uniform(self) -> None:
        def spread(placement):
            scenario = make_scenario.generate_single_cell(9, self.SCENE, 16, mobility="static", placement=placement,
                                                          duration_s=10)
            points = [scenario["nodes"][f"ue{i}"]["start_m"] for i in range(0, 16, 2)]  # all on hotspot 0
            cx = sum(p[0] for p in points) / len(points)
            cy = sum(p[1] for p in points) / len(points)
            return sum(((p[0] - cx) ** 2 + (p[1] - cy) ** 2) ** 0.5 for p in points) / len(points)
        self.assertLess(spread("hotspot"), spread("uniform"))

    def test_prach_preambles_distinct_for_16_ues(self) -> None:
        self.assertEqual(len({bench.single_cell_prach_preamble(i) % 64 for i in range(16)}), 16)
        inner = (BENCH / "run-single-cell-policy-inner.sh").read_text()
        self.assertIn("SRSUE_PRACH_PREAMBLE_INDEX=$((index * 4))", inner)

    def test_attach_channel_equalizes_each_direction(self) -> None:
        profiles = {f"gnb0>ue{i}:sionna_rt": MatrixProfile(1, 1, (LaneProfile(0, 0, (Tap(1.0, -10.0 - 3 * i, 0.5),
                                                                                     Tap(4.0, -20.0 - 3 * i, 1.0))),))
                    for i in range(3)}
        profiles |= {f"ue{i}>gnb0:sionna_rt": MatrixProfile(1, 1, (LaneProfile(0, 0, (Tap(2.0, -5.0 - 6 * i, 0.0),)),))
                     for i in range(3)}
        out = channel_trace.equalized_attach_profiles(profiles, Tap, LaneProfile, MatrixProfile)
        dl_target = 10 * __import__("math").log10(10 ** -1.0 + 10 ** -2.0)  # ue0's two DL taps
        for i in range(3):
            dl, ul = out[f"gnb0>ue{i}:sionna_rt"].lanes[0].taps, out[f"ue{i}>gnb0:sionna_rt"].lanes[0].taps
            self.assertEqual(len(dl), 1)                               # flat
            self.assertAlmostEqual(dl[0].gain_db, dl_target, places=6)  # equal within the direction
            self.assertAlmostEqual(ul[0].gain_db, -5.0)
            self.assertEqual((dl[0].delay_samples, dl[0].phase_rad), (1.0, 0.0))  # first tap's delay, phase 0

    def test_registry_offers_only_validated_counts(self) -> None:
        catalog = scene_registry.catalog(False)
        evidence = scene_registry.load_validation()
        for scene in catalog["scenes"]:
            self.assertEqual(scene["startable_ue_counts"],
                             [n for n in scene_registry.validated_ue_counts(scene["id"], evidence)
                              if n in scene["ue_counts"]] if scene["installed"] else [])
        self.assertEqual(scene_registry.catalog(True)["scenes"][0]["startable_ue_counts"], [1, 2, 4])


class ChannelTraceReplay(unittest.TestCase):
    def test_verify_detects_any_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = fake_channel_bundle(pathlib.Path(tmp))
            self.assertTrue(channel_trace.verify(bundle)["ok"])
            path = bundle / channel_trace.TRACE_FILE
            original = path.read_bytes()
            # One digit of one tap gain: the request bytes differ.
            changed = original.replace(b'gain_db\\":-10.05', b'gain_db\\":-10.06', 1)
            self.assertNotEqual(changed, original)
            path.write_bytes(changed)
            result = channel_trace.verify(bundle)
            self.assertFalse(result["ok"])
            self.assertTrue(any("sha256" in e or "digest" in e for e in result["errors"]), result)
            # Two records swapped: sizes and hashes of the file agree with nothing.
            lines = original.splitlines(keepends=True)
            lines[2], lines[3] = lines[3], lines[2]
            path.write_bytes(b"".join(lines))
            self.assertFalse(channel_trace.verify(bundle)["ok"])

    def test_replay_sends_the_stored_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = fake_channel_bundle(pathlib.Path(tmp))
            for record in channel_trace.iter_records(bundle):
                for text in record["messages"]:
                    message = json.loads(text)
                    self.assertEqual(channel_trace.canonical(message), text)
                    self.assertEqual(message["batch_id"], "attach" if record["k"] is None else channel_trace.batch_id(record["k"]))


class TrafficTraceEquality(unittest.TestCase):
    def test_same_inputs_same_bytes_and_independent_ues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            a = traffic_trace.build(bench.single_cell_profile("mixed", 2), 7, 5.0, tmp / "a")
            b = traffic_trace.build(bench.single_cell_profile("mixed", 2), 7, 5.0, tmp / "b")
            self.assertEqual(a["trace_sha256"], b["trace_sha256"])
            c = traffic_trace.build(bench.single_cell_profile("mixed", 4), 7, 5.0, tmp / "c")
            ue1_two = [r for r in traffic_trace.iter_records(tmp / "a") if r[5] == 1]
            ue1_four = [r for r in traffic_trace.iter_records(tmp / "c") if r[5] == 1]
            self.assertEqual(ue1_two, ue1_four)  # interactive ue1 is unchanged by adding ue2, ue3
            self.assertTrue(traffic_trace.verify(tmp / "a")["ok"])
            os.chmod(tmp / "a" / traffic_trace.TRACE_FILE, 0o644)
            data = bytearray((tmp / "a" / traffic_trace.TRACE_FILE).read_bytes())
            data[20] ^= 1
            (tmp / "a" / traffic_trace.TRACE_FILE).write_bytes(bytes(data))
            self.assertFalse(traffic_trace.verify(tmp / "a")["ok"])


class FairnessAudit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.channel = fake_channel_bundle(root)
        traffic_trace.build(bench.single_cell_profile("mixed", 2), 1, 2.0, root / "traffic")
        self.traffic = root / "traffic"
        self.session = {"scene_id": "sutd_1gnb", "ue_count": 2, "seed": 1}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def runs(self, **mutate):
        runs = [fake_run("01-test1", "test1", self.channel, self.traffic, 100 * 23040),
                fake_run("02-test2", "test2", self.channel, self.traffic, 777 * 23040)]
        for key, fn in mutate.items():
            fn(runs)
        return runs

    def audit(self, runs):
        return validation.audit(self.session, runs, self.channel, self.traffic)

    def test_perfect_evidence_passes_with_different_s0(self) -> None:
        result = self.audit(self.runs())
        self.assertEqual(result["verdict"], "STRICT FAIR COMPARISON: PASS", result["reasons"])
        self.assertTrue(result["strict_identical_external_conditions"])

    def test_each_corruption_fails_its_gate(self) -> None:
        def drop(runs):
            info = runs[1]["report"]["evidence"]["applied"]["gnb0>ue0:sionna_rt"]
            info.update(missing=[4], missing_count=1, updates=info["updates"] - 1, exact=info["exact"] - 1,
                        relative_ok=info["relative_ok"] - 1)
            del info["digests"]["4"], info["sha16"]["4"]

        def shift(runs):
            info = runs[1]["report"]["evidence"]["applied"]["ue1>gnb0:sionna_rt"]
            info.update(exact=info["exact"] - 1, off_target=[{"k": 7, "sample": 1, "target": 0}])

        def corrupt(runs):
            runs[1] = fake_run("02-test2", "test2", self.channel, self.traffic, 777 * 23040, digest_salt="x")

        def foreign(runs):
            runs[0]["report"]["evidence"]["applied"]["gnb0>ue1:sionna_rt"]["sha16"]["5"] = "0" * 16

        def traffic_changed(runs):
            runs[1]["report"]["evidence"]["traffic"]["offered_relative_sha256"] = "other"

        def lagged(runs):
            runs[0]["report"]["evidence"]["traffic"]["lag_samples"]["p99"] = 10 ** 6

        def noise(runs):
            runs[0]["report"]["evidence"]["noise_epochs"]["ue1"]["sample"] += 1

        def crashed(runs):
            runs[1]["gate"]["status"] = "failed"

        cases = {"RADIO ALIGNMENT VERIFIED": [drop, shift, noise], "TRACE VERIFIED": [corrupt, foreign],
                 "TRAFFIC VERIFIED": [traffic_changed, lagged], "RUN VALID": [crashed]}
        for gate, fns in cases.items():
            for fn in fns:
                result = self.audit(self.runs(m=fn))
                self.assertFalse(result["gates"][gate], (gate, fn.__name__))
                self.assertEqual(result["verdict"], "UNQUALIFIED")
                # The other gates are not collateral damage of one corruption.
                others = [g for g, ok in result["gates"].items() if not ok and g != gate]
                self.assertEqual(others, [], (fn.__name__, result["reasons"]))

    def test_one_policy_or_one_run_is_not_a_comparison(self) -> None:
        result = self.audit(self.runs()[:1])
        self.assertFalse(result["gates"]["RUN VALID"])

    def test_aa_control_does_not_require_identical_outcomes(self) -> None:
        def report(values):
            segs = [{"k": k, "valid": True, "dl_mbps": v, "jain_dl": 1.0, "dl_prb_util": 0.5, "decision_us_mean": 5.0,
                     "ues": {"ue0": {"dl_mbps": v, "dl_prb_share": 1.0}}} for k, v in enumerate(values)]
            return {"segments": segs, "metrics": {"aggregate_throughput_mbps": {"dl": sum(values) / len(values), "ul": 1},
                                                  "jain_fairness": {"dl_throughput": 1, "dl_prb_share": 1},
                                                  "resource_utilization": {"dl_prb": 0.5},
                                                  "decision_latency_us": {"mean": 5},
                                                  "per_ue": {"ue0": {"role": "bulk", "five_qi": 9,
                                                                     "throughput_mbps": {"dl": 1, "ul": 1},
                                                                     "dl_prb_share": 1, "pdb_violation_rate": 0,
                                                                     "drop_rate": 0,
                                                                     "delay_radio_ms": {"dl": {"p95": 1, "p99": 2}},
                                                                     "link": {"dl_mcs": 20, "cqi": 15}}}}}
        rr, qos = {"scheduler": "rr"}, {"scheduler": "qos"}
        runs = [{"run_id": "01-test1", "arm": "test1", "test": rr, "report": report([10.0] * 40)},
                {"run_id": "02-test2", "arm": "test2", "test": qos, "report": report([12.0] * 40)},
                {"run_id": "03-test2", "arm": "test2", "test": qos, "report": report([11.0] * 40)},
                {"run_id": "04-test1", "arm": "test1", "test": rr, "report": report([10.5] * 40)}]
        cmp = validation.compare(runs)
        self.assertAlmostEqual(cmp["aa_control"]["test1"]["aggregate_dl_mbps"]["mean_diff"], -0.5)
        self.assertEqual(list(cmp["tests"]), ["test1", "test2"])
        self.assertNotIn("A/A control", cmp["paired"]["note"])
        # The same scheduler on both sides is a valid session, reported as an A/A control.
        same = [dict(run, test=rr) for run in runs]
        self.assertIn("A/A control", validation.compare(same)["paired"]["note"])
        self.assertAlmostEqual(cmp["paired"]["aggregate_dl_mbps"]["mean_diff"], 10.25 - 11.5)


class ExecutionOrder(unittest.TestCase):
    def test_orders(self) -> None:
        arms = bench.TEST_ARMS
        self.assertEqual(single_cell_runner.execution_order(arms, 2, "abba"), ["test1", "test2", "test2", "test1"])
        self.assertEqual(single_cell_runner.execution_order(arms, 2, "alternate"), ["test1", "test2", "test1", "test2"])
        self.assertEqual(single_cell_runner.execution_order(arms, 2, "sequential"), ["test1", "test1", "test2", "test2"])
        self.assertEqual(single_cell_runner.execution_order(arms, 1, "abba"), ["test1", "test2"])


DUMMY_RUNNER = """
import json, os, pathlib, signal, subprocess, sys, time
root = pathlib.Path(sys.argv[sys.argv.index("--session-root") + 1])
session = json.loads((root / "session.json").read_text())
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                         env=dict(os.environ, SINGLE_CELL_RUN_ROOT=str(root / "policy_runs" / "01-test1")))
(root / "child.pid").write_text(str(child.pid))
def write(state):
    (root / "status.json").write_text(json.dumps({"session_id": session["session_id"],
        "generation": session["generation"], "state": state, "message": state}))
signal.signal(signal.SIGTERM, lambda *_: (write("STOPPED"), sys.exit(0)))
write("RUNNING_POLICY")
while True:
    time.sleep(0.1)
"""


class ResetLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        native = root / "native"
        (native / "builds/ocudu-zmq-release/apps/gnb").mkdir(parents=True)
        (native / "builds/ocudu-zmq-release/apps/gnb/gnb").write_text("")
        runner = root / "dummy_runner.py"
        runner.write_text(DUMMY_RUNNER)
        self.saved_runner = controller.RUNNER
        self.saved_grace = controller.CLEANUP_GRACE_S
        controller.RUNNER = runner
        controller.CLEANUP_GRACE_S = 1.0
        self.sessions = root / "sessions"
        self.outside = root / "outside.txt"
        self.outside.write_text("keep")
        self.ctl = controller.Controller(self.sessions, native, sys.executable, allow_unvalidated=True)

    def tearDown(self) -> None:
        try:
            self.ctl.reset()
        finally:
            controller.RUNNER = self.saved_runner
            controller.CLEANUP_GRACE_S = self.saved_grace
            self.tmp.cleanup()

    def wait_state(self, state: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.ctl.status().get("state") == state:
                return
            time.sleep(0.05)
        self.fail(f"never reached {state}: {self.ctl.status()}")

    def test_start_replaces_and_clears_the_previous_session(self) -> None:
        first = self.ctl.start({"ue_count": 1})
        self.wait_state("RUNNING_POLICY")
        child = int((self.sessions / "active" / "child.pid").read_text())
        (self.sessions / "active" / "policy_runs" / "marker").write_text("old data")
        second = self.ctl.start({"ue_count": 4, "placement": "hotspot", "mobility": "pedestrian"})
        self.assertGreater(second["generation"], first["generation"])
        self.assertTrue(second["old_results_cleared"])
        self.assertEqual(second["replaced_session_id"], first["session_id"])
        self.assertFalse((self.sessions / "active" / "policy_runs" / "marker").exists())
        self.assertFalse(pathlib.Path(f"/proc/{child}").exists() and
                         "Z" not in pathlib.Path(f"/proc/{child}/stat").read_text().split()[2])
        self.assertEqual(json.loads((self.sessions / "active" / "session.json").read_text())["ue_count"], 4)
        self.assertEqual(self.outside.read_text(), "keep")

    def test_invalid_request_leaves_the_running_session_alone(self) -> None:
        first = self.ctl.start({"ue_count": 1})
        self.wait_state("RUNNING_POLICY")
        with self.assertRaises(controller.ValidationError) as caught:
            self.ctl.start({"scene_id": "robot_ring", "ue_count": 1})
        self.assertIn("scene_id", caught.exception.errors)
        with self.assertRaises(controller.ValidationError):
            self.ctl.start({"ue_count": 1, "tests": [{"scheduler": "rr"}]})
        with self.assertRaises(controller.ValidationError) as caught:
            self.ctl.start({"ue_count": 1, "tests": [{"scheduler": "rr"}, {"scheduler": "pf", "extra": 1}]})
        self.assertIn("tests", caught.exception.errors)
        self.assertEqual(self.ctl.status()["session_id"], first["session_id"])
        self.assertTrue(self.ctl.status()["runner_alive"])

    def test_same_scheduler_in_both_tests_is_accepted(self) -> None:
        started = self.ctl.start({"ue_count": 1, "tests": [{"scheduler": "qos"}, {"scheduler": "qos"}]})
        self.assertEqual(started["accepted_config"]["tests"], [{"scheduler": "qos"}, {"scheduler": "qos"}])
        options = self.ctl.options()
        self.assertEqual([a["id"] for a in options["tests"]["arms"]], ["test1", "test2"])
        self.assertEqual(options["tests"]["fields"], {"scheduler": list(bench.SCHEDULERS)})

    def test_stop_and_reset(self) -> None:
        self.ctl.start({"ue_count": 1})
        self.wait_state("RUNNING_POLICY")
        child = int((self.sessions / "active" / "child.pid").read_text())
        stopped = self.ctl.stop()
        self.assertTrue(stopped["stopped"])
        self.assertEqual(self.ctl.status()["state"], "STOPPED")
        time.sleep(0.2)
        self.assertEqual(self.ctl._session_processes(self.sessions / "active"), [], "leaked session process")
        del child
        generation = self.ctl.generation
        reset = self.ctl.reset()
        self.assertEqual(reset["state"], "IDLE")
        self.assertGreater(reset["generation"], generation)
        self.assertFalse((self.sessions / "active").exists())

    def test_a_process_that_only_names_the_directory_is_not_touched(self) -> None:
        self.ctl.start({"ue_count": 1})
        self.wait_state("RUNNING_POLICY")
        bystander = subprocess.Popen([sys.executable, "-c", "import time, sys; time.sleep(60)",
                                      str(self.sessions / "active" / "runtime" / "runner.log")])
        try:
            self.ctl.stop()
            self.assertIsNone(bystander.poll(), "a viewer of the session's files was killed")
        finally:
            bystander.kill()
            bystander.wait()

    def test_concurrent_starts_make_one_session(self) -> None:
        results, errors = [], []

        def go():
            try:
                results.append(self.ctl.start({"ue_count": 1}))
            except controller.TransitionError as error:
                errors.append(error)

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(len(results) + len(errors), 4)
        live = [p for p in self.ctl._session_processes(self.sessions / "active")]
        runners = [p for p in live if b"dummy_runner" in pathlib.Path(f"/proc/{p}/cmdline").read_bytes()]
        self.assertEqual(len(runners), 1, "exactly one runner may be alive")

    def test_clear_refuses_a_symlinked_active_directory(self) -> None:
        target = pathlib.Path(self.tmp.name) / "elsewhere"
        target.mkdir()
        (target / "precious").write_text("x")
        (self.sessions / "active").symlink_to(target)
        with self.assertRaises(controller.TransitionError):
            self.ctl.reset()
        self.assertTrue((target / "precious").exists())
        (self.sessions / "active").unlink()


class ControlServerHttp(unittest.TestCase):
    """The HTTP layer: CSRF guard, generation tags, validation errors."""

    def setUp(self) -> None:
        # By path: other test modules put a different `server` on sys.path.
        import importlib.util
        spec = importlib.util.spec_from_file_location("scheduler_benchmark_server", BENCH / "server.py")
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        self.lifecycle = ResetLifecycle("test_stop_and_reset")
        self.lifecycle.setUp()
        ctl = self.lifecycle.ctl
        handler = server.make_handler(server.Runs(pathlib.Path(self.lifecycle.tmp.name)), server.SystemSampler(), False,
                                      ctl, controller.Results(ctl))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.lifecycle.tearDown()

    def call(self, method: str, path: str, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=20)
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        response = conn.getresponse()
        return response.status, json.loads(response.read() or b"{}")

    def test_api(self) -> None:
        ui = {"X-Benchmark-Client": "ui", "Content-Type": "application/json"}
        status, body = self.call("POST", "/api/benchmark/start", {"ue_count": 1}, {"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        status, body = self.call("POST", "/api/benchmark/start", {"ue_count": 1},
                                 {**ui, "Origin": "http://attacker.example"})
        self.assertEqual(status, 403)
        status, body = self.call("POST", "/api/benchmark/start", {"ue_count": 1, "seed": -1, "placement": "ring"}, ui)
        self.assertEqual(status, 400)
        self.assertIn("seed", body["fields"])
        self.assertIn("placement", body["fields"])
        status, body = self.call("POST", "/api/benchmark/start", {"ue_count": 2, "placement": "hotspot"}, ui)
        self.assertEqual(status, 202)
        generation = body["generation"]
        for path in ("/api/benchmark/status", "/api/benchmark/results", "/api/benchmark/scene"):
            status, body = self.call("GET", path)
            self.assertEqual(status, 200, path)
            self.assertEqual(body["generation"], generation, path)
        status, body = self.call("POST", "/api/benchmark/reset", {}, ui)
        self.assertEqual(status, 200)
        self.assertGreater(body["generation"], generation)
        status, body = self.call("GET", "/api/benchmark/results")
        self.assertEqual(body["runs"], [])


class UiGenerationIsolation(unittest.TestCase):
    """Static checks of the shipped page: every polled response passes the
    generation guard, and a start clears the page before the request returns."""

    page = (BENCH / "index.html").read_text()

    def test_every_poll_response_goes_through_accept(self) -> None:
        polls = re.findall(r"await api\('(/api/benchmark/(?:status|results|scene))'\);\n\s*if \(accept\(", self.page)
        self.assertEqual(sorted(set(polls)), ["/api/benchmark/results", "/api/benchmark/scene", "/api/benchmark/status"])
        self.assertIn("return body.generation === state.generation;", self.page)

    def test_start_clears_before_awaiting_and_aborts_old_requests(self) -> None:
        start = self.page[self.page.index("async function startBenchmark"):self.page.index("async function simple")]
        self.assertLess(start.index("clearView()"), start.index("await post('/api/benchmark/start'"))
        self.assertIn("state.controller.abort()", start)
        clear = self.page[self.page.index("function clearView"):self.page.index("// ---------------------------------------------------------------- controls")]
        for element in ("runs", "comparison", "chanChart", "gates", "sceneOverlay"):
            self.assertIn(f"$('{element}')", clear)

    def test_two_test_panels_built_from_server_fields(self) -> None:
        self.assertIn('id="tests"', self.page)
        self.assertIn("tests: testSelection()", self.page)
        self.assertIn("Object.entries(o.tests.fields)", self.page)
        self.assertNotIn("schedulers", self.page)

    def test_no_follow_latest_in_control_mode(self) -> None:
        self.assertNotIn("follow_latest", self.page)


if __name__ == "__main__":
    unittest.main()
