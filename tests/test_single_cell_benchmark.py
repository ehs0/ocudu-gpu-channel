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
BENCH = PROJECT_ROOT / "examples" / "ran_benchmark"
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


class GnbAntennas(unittest.TestCase):
    """The 2-antenna gNB: the scenario array, the broker ports and the 2T2R cell."""

    def setUp(self) -> None:
        import render_single_cell
        self.render = render_single_cell

    def test_scenario_array_and_unchanged_ues(self) -> None:
        one = make_scenario.generate_single_cell(3, "sutd_1gnb", 4, mobility="pedestrian", placement="hotspot",
                                                 duration_s=40)
        two = make_scenario.generate_single_cell(3, "sutd_1gnb", 4, mobility="pedestrian", placement="hotspot",
                                                 duration_s=40, gnb_antennas=2)
        self.assertEqual(two["nodes"]["gnb0"]["array"], {"rows": 1, "cols": 2, "pattern": "iso", "polarization": "V"})
        self.assertEqual(one["nodes"]["gnb0"]["array"]["cols"], 1)
        self.assertEqual({n: v for n, v in one["nodes"].items() if n != "gnb0"},
                         {n: v for n, v in two["nodes"].items() if n != "gnb0"})
        self.assertEqual((one["benchmark"]["gnb_antennas"], two["benchmark"]["gnb_antennas"]), (1, 2))
        self.assertEqual(self.render.check_scenario(two, 4), 2)
        with self.assertRaises(ValueError):
            make_scenario.generate_single_cell(3, "sutd_1gnb", 1, mobility="static", placement="uniform",
                                               duration_s=10, gnb_antennas=4)

    def test_topology_has_one_port_per_antenna_with_its_own_floor(self) -> None:
        text = self.render.render_topology(2, {"gnb0": 1e-2, "ue0": 1e-3, "ue1": 1e-3}, 2)
        self.assertIn("  - id: gnb0_p1\n    role: port\n    sample_rate_hz: 23040000\n"
                      "    tx_endpoint: tcp://127.0.0.1:2002\n    rx_endpoint: tcp://127.0.0.1:2003\n"
                      "    rx_model: rx_noise_gnb0\n", text)
        self.assertIn("  - id: gnb0\n    tx_ports:\n      - gnb0_p0\n      - gnb0_p1\n"
                      "    rx_ports:\n      - gnb0_p0\n      - gnb0_p1\n", text)
        self.assertIn("  - id: ue1\n    tx_ports:\n      - ue1_p0\n    rx_ports:\n      - ue1_p0\n", text)
        self.assertEqual(self.render.render_topology(2, {}, 1), self.render.render_topology(2, {}))

    def test_2t2r_cell_adaptations(self) -> None:
        source = (PROJECT_ROOT / "examples/configs/ran/ocudu/docker/gnb_zmq_b210_fdd_srsue.yaml").read_text()
        siso = self.render.multi_gnb.render_gnb(source, self.render.GNB_CELL, pathlib.Path("/logs"))
        self.assertEqual(self.render.render_gnb_antennas(siso, 1), siso)
        text = self.render.render_gnb_antennas(siso, 2)
        for token in ("device_args: tx_port0=tcp://127.0.0.1:2000,tx_port1=tcp://127.0.0.1:2002,"
                      "rx_port0=tcp://127.0.0.1:2001,rx_port1=tcp://127.0.0.1:2003,base_srate=23.04e6\n",
                      "  nof_antennas_dl: 2\n", "  nof_antennas_ul: 2\n", "      ss2_type: ue_dedicated\n",
                      "      dci_format_0_1_and_1_1: true\n", "    csi_rs_enabled: false\n",
                      "    nof_cell_csi_res: 0\n", "  mac_enable: disable\n", "  rx_gain: 0\n"):
            self.assertEqual(text.count(token), 1, token)
        # The policy block still goes in exactly once.
        self.assertEqual(self.render.insert_scheduler_policy(text, "rr").count("rr_sched:"), 1)

    def test_multi_lane_evidence_is_keyed_by_link(self) -> None:
        import single_cell_analyze
        line = ("event=control_sample_applied link_id=ue0>gnb0:sionna_rt#r0t0 tag=k=3;sha=0123456789abcdef seqno=5 "
                "slot=9 sample=100 target_sample=100 count=23040 digest=00000000000000aa unix_ns=1")
        link_id = single_cell_analyze.APPLIED_LINE.match(line).group(1)
        self.assertEqual(single_cell_analyze.LANE_SUFFIX.match(link_id).group(1), single_cell_analyze.GNB_REF_LINK)
        self.assertIsNone(single_cell_analyze.LANE_SUFFIX.match("ue0>gnb0:sionna_rt"))

    def test_evidence_is_per_antenna_count(self) -> None:
        validation = {"validated": [{"scene_id": "sutd_1gnb", "ue_count": 2},
                                    {"scene_id": "sutd_1gnb", "ue_count": 4, "gnb_antennas": 2}]}
        self.assertEqual(scene_registry.validated_ue_counts("sutd_1gnb", validation, 1), [2])
        self.assertEqual(scene_registry.validated_ue_counts("sutd_1gnb", validation, 2), [4])
        self.assertEqual(scene_registry.validated_ue_counts("sutd_1gnb", validation), [2, 4])
        # Startable counts do not depend on the antenna count: 2-antenna evidence opens the count too.
        self.assertIn(4, scene_registry.catalog(False)["scenes"][0]["startable_ue_counts"])


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


class CellKpis(unittest.TestCase):
    def test_pooled_worst_and_efficiency(self) -> None:
        def ue(dl, wait, mean, count, p99, offered, drop, bler):
            return {"throughput_mbps": {"dl": dl}, "starvation": {"dl_wait_ms": {"p99": wait}},
                    "delay_radio_ms": {"dl": {"mean": mean, "count": count, "p95": p99 - 1, "p99": p99}},
                    "link": {"dl_mcs": 10, "dl_bler": bler, "ul_bler": 0.0},
                    "offered_packets": offered, "drop_rate": drop, "pdb_violation_rate": drop}
        m = {"aggregate_throughput_mbps": {"mac_dl": 18.0}, "resource_utilization": {"dl_prb": 0.5, "nof_prb": 100},
             "per_ue": {"ue0": ue(10.0, 2.0, 10.0, 300, 20.0, 100, 0.0, 0.01),
                        "ue1": ue(2.0, 9.0, 40.0, 100, 90.0, 300, 0.2, 0.03)}}
        k = validation.cell_kpis(m)
        self.assertEqual((k["worst_ue_dl_mbps"], k["worst_ue_dl_wait_p99_ms"], k["worst_ue_dl_delay_p99_ms"]),
                         (2.0, 9.0, 90.0))
        self.assertAlmostEqual(k["dl_delay_mean_ms"], (10 * 300 + 40 * 100) / 400)   # weighted by received packets
        self.assertAlmostEqual(k["drop_rate"], 0.2 * 300 / 400)                      # weighted by offered packets
        self.assertAlmostEqual(k["dl_bler"], 0.02)
        self.assertAlmostEqual(k["dl_spectral_efficiency_bps_hz"], 18e6 / (50 * 180e3))
        # Older reports without these fields give None, not an error.
        self.assertIsNone(validation.cell_kpis({"per_ue": {"ue0": {}}})["dl_delay_mean_ms"])


class SchedulerIdentity(unittest.TestCase):
    def test_config_dump_names_exactly_one_scheduler(self) -> None:
        block = lambda name: "".join(f"      {line}\n" for line in bench.SCHEDULERS[name]["yaml"].splitlines()[2:])
        for name in bench.SCHEDULERS:
            self.assertEqual(bench.schedulers_in_config_dump(block(name)), [name])
        self.assertEqual(bench.schedulers_in_config_dump("      rr_sched: ~\n"), ["rr"])
        # Numbers compare by value; a changed parameter is no known scheduler.
        self.assertEqual(bench.schedulers_in_config_dump(block("maxci").replace("0.0", "0")), ["maxci"])
        self.assertEqual(bench.schedulers_in_config_dump(block("qos").replace("2.0", "1.0")), [])


class LinkAdaptation(unittest.TestCase):
    """Fixed MCS, CQI/SINR AMC and AMC + OLLA: request, gNB YAML and config-dump identity."""

    FIXED = {"link_adaptation": "fixed", "fixed_dl_mcs": 10, "fixed_ul_mcs": 20}

    @staticmethod
    def dump(test: dict) -> tuple[str, str]:
        """The pdsch / pusch blocks of a gNB config dump of the rendered keys."""

        lines = bench.link_adaptation_yaml(test)
        return tuple("    mcs_table: qam64\n" + lines[section] for section in ("pdsch", "pusch"))

    def test_keys_per_mode(self) -> None:
        fixed = bench.link_adaptation_parameters(self.FIXED)
        self.assertEqual(fixed["pdsch"], {"min_ue_mcs": 10, "max_ue_mcs": 10, "olla_cqi_inc_step": 0})
        self.assertEqual(fixed["pusch"], {"min_ue_mcs": 20, "max_ue_mcs": 20, "olla_snr_inc_step": 0})
        amc = bench.link_adaptation_parameters({"link_adaptation": "amc"})
        self.assertEqual((amc["pdsch"]["max_ue_mcs"], amc["pdsch"]["olla_cqi_inc_step"],
                          amc["pusch"]["olla_snr_inc_step"]), (28, 0, 0))
        self.assertEqual(bench.link_adaptation_parameters({"link_adaptation": "amc_olla"}),
                         bench.LINK_ADAPTATION_DEFAULTS)
        # HARQ-driven OLLA differs from AMC + OLLA in the DL CQI offset range alone (CQI 15 -> 1).
        harq = bench.link_adaptation_parameters({"link_adaptation": "harq_olla"})
        self.assertEqual(harq["pdsch"], {**bench.LINK_ADAPTATION_DEFAULTS["pdsch"], "olla_max_cqi_offset": 14.0})
        self.assertEqual(harq["pusch"], bench.LINK_ADAPTATION_DEFAULTS["pusch"])
        self.assertEqual(bench.LINK_ADAPTATION_DEFAULTS["pdsch"]["olla_max_cqi_offset"], 4.0)
        for bad in ({"link_adaptation": "pf"}, {"link_adaptation": "fixed", "fixed_dl_mcs": 29, "fixed_ul_mcs": 1},
                    {"link_adaptation": "fixed", "fixed_dl_mcs": 3}):
            with self.assertRaises(ValueError):
                bench.link_adaptation_parameters(bad)

    def test_config_dump_names_exactly_the_rendered_mode(self) -> None:
        for test in (self.FIXED, {"link_adaptation": "amc"}, {"link_adaptation": "amc_olla"},
                     {"link_adaptation": "harq_olla"}, {"link_adaptation": "fixed", "fixed_dl_mcs": 0, "fixed_ul_mcs": 28}):
            self.assertEqual(bench.link_adaptation_in_config_dump(*self.dump(test)), test)
        # Keys the dump leaves out run with OCUDU's defaults: AMC + OLLA.
        self.assertEqual(bench.link_adaptation_in_config_dump("    mcs_table: qam64\n", ""),
                         {"link_adaptation": "amc_olla"})
        pdsch, pusch = self.dump({"link_adaptation": "amc"})
        self.assertEqual(bench.link_adaptation_in_config_dump(pdsch.replace("olla_cqi_inc_step: 0", "olla_cqi_inc_step: 0.0"),
                                                              pusch), {"link_adaptation": "amc"})
        # Half a mode is no mode: OLLA off only on DL, a fixed DL with an adaptive UL, a changed OLLA target.
        self.assertIsNone(bench.link_adaptation_in_config_dump(pdsch, "    mcs_table: qam64\n"))
        fixed_pdsch, _ = self.dump(self.FIXED)
        self.assertIsNone(bench.link_adaptation_in_config_dump(fixed_pdsch, pusch))
        olla_pdsch, olla_pusch = self.dump({"link_adaptation": "amc_olla"})
        self.assertIsNone(bench.link_adaptation_in_config_dump(olla_pdsch.replace("0.01", "0.1"), olla_pusch))

    def test_requests_are_normalized(self) -> None:
        self.assertEqual(bench.normalize_test({"scheduler": "rr"}), {"scheduler": "rr", "link_adaptation": "amc_olla"})
        self.assertEqual(bench.normalize_test({"fixed_ul_mcs": 5, "link_adaptation": "fixed", "scheduler": "qos"}),
                         {"scheduler": "qos", "link_adaptation": "fixed", "fixed_dl_mcs": bench.FIXED_MCS_DEFAULT,
                          "fixed_ul_mcs": 5})
        self.assertEqual(list(bench.normalize_test({"fixed_dl_mcs": 1, "fixed_ul_mcs": 2, "link_adaptation": "fixed",
                                                    "scheduler": "rr"})), list(bench.TEST_FIELDS) + list(bench.TEST_PARAMETERS))
        for bad in ({"scheduler": "rr", "link_adaptation": "amc", "fixed_dl_mcs": 3},
                    {"scheduler": "rr", "link_adaptation": "fixed", "fixed_dl_mcs": 29},
                    {"scheduler": "rr", "link_adaptation": "fixed", "fixed_dl_mcs": True},
                    {"scheduler": "rr", "link_adaptation": "fixed", "fixed_dl_mcs": 3.0},
                    {"scheduler": "rr", "link_adaptation": "olla"}, {"scheduler": "rr", "mcs": 3}):
            with self.assertRaises(ValueError, msg=bad):
                bench.normalize_test(bad)

    def test_labels(self) -> None:
        self.assertEqual(bench.test_label("test1", {"scheduler": "rr", **self.FIXED}), "Test 1 (RR, Fixed MCS 10/20)")
        self.assertEqual(bench.test_label("test2", {"scheduler": "qos", "link_adaptation": "amc_olla"}),
                         "Test 2 (QoS-PF, AMC+OLLA)")
        # A session recorded before the field.
        self.assertEqual(bench.test_label("test2", {"scheduler": "qos"}), "Test 2 (QoS-PF)")

    def test_gnb_yaml_in_both_antenna_cells(self) -> None:
        import render_single_cell
        source = (PROJECT_ROOT / "examples/configs/ran/ocudu/docker/gnb_zmq_b210_fdd_srsue.yaml").read_text()
        siso = render_single_cell.multi_gnb.render_gnb(source, render_single_cell.GNB_CELL, pathlib.Path("/logs"))
        for antennas in (1, 2):
            text = render_single_cell.render_gnb_antennas(siso, antennas)
            out = render_single_cell.insert_link_adaptation(text, self.FIXED)
            self.assertEqual(out.count("    min_ue_mcs: "), 2)
            self.assertIn("  pdsch:\n    mcs_table: qam64\n    min_ue_mcs: 10\n    max_ue_mcs: 10\n"
                          "    olla_cqi_inc_step: 0\n", out)
            self.assertIn("  pusch:\n    mcs_table: qam64\n    min_ue_mcs: 20\n    max_ue_mcs: 20\n"
                          "    olla_snr_inc_step: 0\n", out)
            # Only the link-adaptation lines differ between modes.
            other = render_single_cell.insert_link_adaptation(text, {"link_adaptation": "amc_olla"})
            removed = {line for line in out.splitlines() if line not in other.splitlines()}
            self.assertTrue(all(line.strip().split(":")[0] in bench.LINK_ADAPTATION_DEFAULTS["pdsch"]
                                or line.strip().split(":")[0] in bench.LINK_ADAPTATION_DEFAULTS["pusch"]
                                for line in removed), removed)
            with self.assertRaises(SystemExit):  # the renderer's fail(): keys already set
                render_single_cell.insert_link_adaptation(out, self.FIXED)

    def test_kpis(self) -> None:
        def ue(dl_mcs, ul_mcs, dl_bler, ul_bler):
            return {"link": {"dl_mcs": dl_mcs, "ul_mcs": ul_mcs, "dl_bler": dl_bler, "ul_bler": ul_bler, "cqi": 15,
                             "pusch_snr_db": 30.0}}
        k = validation.cell_kpis({"per_ue": {"ue0": ue(20, 28, 0.01, 0.0), "ue1": ue(10, 24, 0.2, 0.05)}})
        self.assertEqual((k["ul_mcs_mean"], k["worst_ue_dl_bler"], k["worst_ue_ul_bler"], k["cqi_mean"]),
                         (26.0, 0.2, 0.05, 15.0))


class ServiceValidity(unittest.TestCase):
    """A radio second is invalid when a UE is out of service, judged by what it decoded, not by srsUE's DL SNR."""

    def validity(self, side: dict, gnb_rows: list | None = None) -> dict:
        import types
        import single_cell_analyze
        stub = types.SimpleNamespace(
            ue_ids=["ue0"], benchmark={"gnb": {"pci": 1}},
            gnb_metrics={5: {"ue0": gnb_rows if gnb_rows is not None else [{"dl_nof_ok": 90, "dl_nof_nok": 10}]}},
            ue_side={"ue0": {5: {"pci": 1.0, "dl_snr": 20.0, "dl_mcs": 9.0, "dl_bler": 0.0, **side}}},
            sched=types.SimpleNamespace(seg={5: {"first_slot": 0}}))
        return single_cell_analyze.SingleCellRun.segment_validity(stub, 5)

    def test_rules(self) -> None:
        # A notch UE: srsUE reports a negative SNR but decodes (seen in session g37).
        self.assertTrue(self.validity({"dl_snr": -4.8, "dl_bler": 0.7})["valid"])
        # A poor link adaptation stays in the KPIs: 88 % BLER is service, not an outage.
        self.assertTrue(self.validity({"dl_snr": -5.0, "dl_bler": 88.0})["valid"])
        self.assertEqual(self.validity({"dl_bler": 100.0})["reasons"], ["ue0: nothing decoded (srsUE DL BLER 100%)"])
        self.assertEqual(self.validity({"pci": 2.0})["reasons"], ["ue0: out of service (camped on pci=2)"])
        self.assertEqual(self.validity({}, [{"dl_nof_ok": 0, "dl_nof_nok": 30}])["reasons"],
                         ["ue0: every DL HARQ failed"])
        self.assertEqual(self.validity({}, [])["reasons"], ["ue0: not in the gNB report"])


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
        for antennas in (4, True, "2"):
            with self.assertRaises(controller.ValidationError) as caught:
                self.ctl.start({"ue_count": 1, "gnb_antennas": antennas})
            self.assertIn("gnb_antennas", caught.exception.errors)
        self.assertEqual(self.ctl.status()["session_id"], first["session_id"])
        self.assertTrue(self.ctl.status()["runner_alive"])

    def test_same_scheduler_in_both_tests_is_accepted(self) -> None:
        started = self.ctl.start({"ue_count": 1, "tests": [{"scheduler": "qos"}, {"scheduler": "qos"}]})
        # A test without link_adaptation runs OCUDU's default, written into the session.
        self.assertEqual(started["accepted_config"]["tests"], [{"scheduler": "qos", "link_adaptation": "amc_olla"}] * 2)
        options = self.ctl.options()
        self.assertEqual([a["id"] for a in options["tests"]["arms"]], ["test1", "test2"])
        self.assertEqual(options["tests"]["fields"], {"scheduler": list(bench.SCHEDULERS),
                                                      "link_adaptation": list(bench.LINK_ADAPTATION)})
        self.assertEqual(options["tests"]["parameters"]["fixed_dl_mcs"]["when"], {"link_adaptation": "fixed"})
        self.assertEqual([la["id"] for la in options["link_adaptations"]], ["fixed", "amc", "amc_olla", "harq_olla"])

    def test_link_adaptation_per_test(self) -> None:
        tests = [{"scheduler": "qos", "link_adaptation": "fixed", "fixed_dl_mcs": 12, "fixed_ul_mcs": 20},
                 {"scheduler": "qos", "link_adaptation": "amc_olla"}]
        started = self.ctl.start({"ue_count": 1, "tests": tests})
        self.assertEqual(started["accepted_config"]["tests"], tests)
        session = json.loads((self.sessions / "active" / "session.json").read_text())
        self.assertEqual(session["tests"], tests)
        for bad in ([{"scheduler": "qos", "link_adaptation": "amc", "fixed_dl_mcs": 3}, tests[1]],
                    [{"scheduler": "qos", "link_adaptation": "fixed", "fixed_dl_mcs": 40}, tests[1]]):
            with self.assertRaises(controller.ValidationError) as caught:
                self.ctl.start({"ue_count": 1, "tests": bad})
            self.assertIn("tests", caught.exception.errors)
        self.assertEqual(self.ctl.status()["session_id"], started["session_id"])

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
