#!/usr/bin/env python3
"""Fairness audit and the Test 1 / Test 2 comparison across the runs of one session.

Every field of fairness-audit.json is derived from recorded evidence -- the
bundles' hashes, each run's broker log (via single_cell_analyze.evidence())
and gate summary -- never assumed. Four gates:

  TRACE VERIFIED            both bundles re-hash to their manifests; every run
                            replayed the same bundle; for every link and update
                            k the request each broker applied is the trace's
                            (tag sha) and decoded to the same profile digest in
                            every run; the attach channel likewise.
  RADIO ALIGNMENT VERIFIED  in every run, every link applied every update k
                            exactly once, in order, on exactly sample
                            S0 + k * samples_per_update; with noise enabled,
                            every receiver restarted its counter-indexed noise
                            on sample S0.
  TRAFFIC VERIFIED          the same traffic trace; every offered packet was
                            sent, with the same (flow, seq, target - S0) set in
                            every run; injection lag within tolerance.
  RUN VALID                 every UE attached, no crash, measurement completed,
                            broker transport clean, >= 90% of radio seconds valid.

STRICT FAIR COMPARISON: PASS only when all four pass; otherwise UNQUALIFIED
with the failing reasons. Outcomes (throughput, HARQ, delays) are allowed to
differ between runs -- also between repeats of one test, and between two tests
configured identically (scheduler and link adaptation; both are A/A controls).
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import statistics
import sys
from typing import Any, Sequence

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import analyze  # noqa: E402
import channel_trace  # noqa: E402
import traffic_trace  # noqa: E402

# The broker's link clock advances one serve call (~one 1 ms slot of samples)
# at a time and the sender polls it, so a packet is released 0..1 slot after
# its target plus the polling delay. Three slots at p99 and ten at the
# maximum keep injection at slot granularity, the resolution the MAC
# scheduler works at; larger lags fail the traffic gate.
LAG_P99_TOLERANCE_SAMPLES = 3 * 23040    # 3 ms of radio time
LAG_MAX_TOLERANCE_SAMPLES = 10 * 23040   # 10 ms
VALID_SEGMENT_FRACTION = 0.9


def json_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def audit(session: dict[str, Any], runs: list[dict[str, Any]], channel_bundle: pathlib.Path,
          traffic_bundle: pathlib.Path) -> dict[str, Any]:
    """runs: [{"run_id", "arm", "test", "label", "report", "gate", "parameters"}] in execution order."""

    reasons: dict[str, list[str]] = {"trace": [], "alignment": [], "traffic": [], "validity": []}
    channel_check = channel_trace.verify(channel_bundle)
    traffic_check = traffic_trace.verify(traffic_bundle)
    cmanifest = json.loads((channel_bundle / channel_trace.MANIFEST_FILE).read_text(encoding="utf-8"))
    tmanifest = json.loads((traffic_bundle / traffic_trace.MANIFEST_FILE).read_text(encoding="utf-8"))
    updates = int(cmanifest["config"]["updates"])
    noise = cmanifest["config"].get("noise") or {}
    expected_sha16: dict[str, list[str]] = {}
    attach_sha16: dict[str, str] = {}
    for index, record in enumerate(channel_trace.iter_records(channel_bundle)):
        for link_id in record["link_ids"]:
            sha16 = record["link_sha256"][link_id][:16]
            if index == 0:
                attach_sha16[link_id] = sha16
            else:
                expected_sha16.setdefault(link_id, []).append(sha16)
    links = sorted(expected_sha16)

    if not channel_check["ok"]:
        reasons["trace"] += [f"channel bundle: {e}" for e in channel_check["errors"]]
    if not traffic_check["ok"]:
        reasons["traffic"] += [f"traffic bundle: {e}" for e in traffic_check["errors"]]
    if len(runs) < 2:
        reasons["validity"].append("fewer than two runs: nothing to compare")
    arms = sorted({run["arm"] for run in runs})
    if len(arms) < 2:
        reasons["validity"].append("only one test ran: no Test 1 / Test 2 comparison")

    reference_digests: dict[str, dict[str, str]] = {}
    reference_attach: dict[str, str] = {}
    reference_offered: str | None = None
    per_run = []
    for run in runs:
        report, gate, params = run.get("report") or {}, run.get("gate") or {}, run.get("parameters") or {}
        label = run["run_id"]
        evidence = report.get("evidence") or {}
        applied = evidence.get("applied") or {}
        epoch = report.get("epoch")
        row = {"run_id": label, "arm": run["arm"], "test": run.get("test"), "s0": (epoch or {}).get("s0")}
        if params.get("channel_bundle") and pathlib.Path(params["channel_bundle"]).resolve() != channel_bundle.resolve():
            reasons["trace"].append(f"{label}: replayed a different channel bundle")
        if params.get("traffic_bundle") and pathlib.Path(params["traffic_bundle"]).resolve() != traffic_bundle.resolve():
            reasons["traffic"].append(f"{label}: offered a different traffic bundle")
        if epoch is None:
            reasons["alignment"].append(f"{label}: no measurement epoch (S0) was recorded")
        if epoch and epoch.get("trace_sha256") != cmanifest["trace_sha256"]:
            reasons["trace"].append(f"{label}: epoch names trace {epoch.get('trace_sha256')}")
        if evidence.get("malformed_evidence_lines"):
            reasons["alignment"].append(f"{label}: {evidence['malformed_evidence_lines']} broker evidence lines "
                                        "could not be parsed")
        if evidence.get("duplicate_applies"):
            reasons["alignment"].append(f"{label}: {evidence['duplicate_applies']} updates applied twice")
        exact_total = 0
        for link_id in links:
            info = applied.get(link_id)
            if info is None:
                reasons["alignment"].append(f"{label}: {link_id} applied no trace update")
                continue
            if info["missing_count"]:
                reasons["alignment"].append(f"{label}: {link_id} missed {info['missing_count']} updates "
                                            f"(first {info['missing'][:5]})")
            if info["exact"] != info["updates"]:
                reasons["alignment"].append(f"{label}: {link_id} applied {info['updates'] - info['exact']} updates "
                                            f"off their target sample (e.g. {info['off_target'][:2]})")
            if info["relative_ok"] != info["updates"]:
                reasons["alignment"].append(f"{label}: {link_id} targets are not S0 + k*period")
            if not info["monotonic"]:
                reasons["alignment"].append(f"{label}: {link_id} updates applied out of order")
            exact_total += info["exact"]
            foreign = [k for k, sha in info["sha16"].items() if sha != expected_sha16[link_id][int(k)]]
            if foreign:
                reasons["trace"].append(f"{label}: {link_id} applied {len(foreign)} requests that are not the "
                                        f"trace's (e.g. k={foreign[:3]})")
            attach = info.get("attach")
            if attach is None:
                reasons["trace"].append(f"{label}: {link_id} has no applied attach channel")
            else:
                if attach["sha16"] != attach_sha16.get(link_id):
                    reasons["trace"].append(f"{label}: {link_id} attach request is not the trace's")
                if link_id in reference_attach and attach["digest"] != reference_attach[link_id]:
                    reasons["trace"].append(f"{label}: {link_id} attach decoded differently")
                reference_attach.setdefault(link_id, attach["digest"])
            # Compared per update index: a missing update is an alignment
            # failure above, not a reason to misalign every later digest.
            if link_id in reference_digests:
                ref = reference_digests[link_id]
                diff = [k for k, digest in info["digests"].items() if k in ref and ref[k] != digest]
                if diff:
                    reasons["trace"].append(f"{label}: {link_id} decoded profiles differ from the first run "
                                            f"at {len(diff)} updates (e.g. k={diff[:3]})")
            else:
                reference_digests[link_id] = dict(info["digests"])
        row["applied_exact"] = exact_total
        row["applied_expected"] = updates * len(links)
        if noise.get("enabled"):
            nodes = ["gnb0"] + [f"ue{i}" for i in range(report.get("ue_count") or 0)]
            epochs = evidence.get("noise_epochs") or {}
            for node in nodes:
                got = epochs.get(node)
                if got is None:
                    reasons["alignment"].append(f"{label}: {node} logged no receiver-noise epoch")
                elif epoch and (got["sample"] != epoch["s0"] or not got["applied"]):
                    reasons["alignment"].append(f"{label}: {node} noise epoch at {got['sample']} "
                                                f"(S0 {epoch['s0']}, applied={got['applied']})")
            if evidence.get("noise_missed"):
                reasons["alignment"].append(f"{label}: noise epoch missed {evidence['noise_missed'][:2]}")
        tr = evidence.get("traffic") or {}
        if tr.get("sent_packets") != tr.get("expected_packets"):
            reasons["traffic"].append(f"{label}: sent {tr.get('sent_packets')} of {tr.get('expected_packets')} "
                                      "offered packets")
        lag = tr.get("lag_samples") or {}
        if lag.get("p99") is None or lag["p99"] > LAG_P99_TOLERANCE_SAMPLES:
            reasons["traffic"].append(f"{label}: injection lag p99 {lag.get('p99')} samples > "
                                      f"{LAG_P99_TOLERANCE_SAMPLES}")
        if lag.get("max") is not None and lag["max"] > LAG_MAX_TOLERANCE_SAMPLES:
            reasons["traffic"].append(f"{label}: injection lag max {lag['max']} samples > {LAG_MAX_TOLERANCE_SAMPLES}")
        if reference_offered is None:
            reference_offered = tr.get("offered_relative_sha256")
        elif tr.get("offered_relative_sha256") != reference_offered:
            reasons["traffic"].append(f"{label}: offered (flow, seq, target - S0) set differs from the first run")
        row["traffic_lag_samples"] = lag
        if gate.get("status") != "passed":
            failed = [k for k in ("all_ues_attached", "broker_clean") if not gate.get(k)]
            reasons["validity"].append(f"{label}: gate {gate.get('status', 'missing')} "
                                       f"(measure={gate.get('measure_status')}, {', '.join(failed) or 'see gate'})")
        complete, valid = report.get("segments_complete") or 0, report.get("segments_valid") or 0
        if complete < report.get("measure_seconds", 0):
            reasons["validity"].append(f"{label}: {complete} of {report.get('measure_seconds')} radio seconds measured")
        if complete and valid / complete < VALID_SEGMENT_FRACTION:
            reasons["validity"].append(f"{label}: only {valid}/{complete} radio seconds valid")
        row["segments_valid"], row["segments_complete"] = valid, complete
        row["real_time_ratio"] = (report.get("runtime") or {}).get("real_time_ratio")
        per_run.append(row)

    gates = {
        "TRACE VERIFIED": not reasons["trace"],
        "RADIO ALIGNMENT VERIFIED": not reasons["alignment"],
        "TRAFFIC VERIFIED": not reasons["traffic"],
        "RUN VALID": not reasons["validity"],
    }
    strict = all(gates.values())
    return {
        "channel_trace_hash": f"sha256:{cmanifest['trace_sha256']}",
        "traffic_trace_hash": f"sha256:{tmanifest['trace_sha256']}",
        "channel_trace_rehash": f"sha256:{channel_check.get('trace_sha256')}",
        "traffic_trace_rehash": f"sha256:{traffic_check.get('trace_sha256')}",
        "tests": {run["arm"]: run.get("test") for run in runs},
        "test_labels": {run["arm"]: run.get("label", run["arm"]) for run in runs},
        "aa_control": len({json_key(run.get("test")) for run in runs}) == 1 and len(arms) == 2,
        "run_order": [run["arm"] for run in runs],
        "scene_id": session.get("scene_id"),
        "ue_count": session.get("ue_count"),
        "seed": session.get("seed"),
        "updates_per_link": updates,
        "links": links,
        "all_channel_batches_identical": not reasons["trace"],
        "all_channel_update_indices_aligned": not [r for r in reasons["alignment"] if "noise" not in r],
        "all_noise_inputs_identical": (None if not noise.get("enabled") else
                                       not [r for r in reasons["alignment"] if "noise" in r]),
        "noise": {"enabled": bool(noise.get("enabled")), "config_sha256": cmanifest.get("noise_sha256"),
                  "basis": ("counter-indexed Philox noise restarted at S0 on every receiver (broker log); bit-exact "
                            "restart is unit-tested in tests/core/test_sample_indexed_control.cpp") if noise.get("enabled")
                  else "receiver noise disabled"},
        "all_offered_traffic_identical": not reasons["traffic"],
        "all_radio_runs_valid": not reasons["validity"],
        "gates": gates,
        "strict_identical_external_conditions": gates["TRACE VERIFIED"] and gates["RADIO ALIGNMENT VERIFIED"]
                                                and gates["TRAFFIC VERIFIED"],
        "verdict": "STRICT FAIR COMPARISON: PASS" if strict else "UNQUALIFIED",
        "reasons": {key: value[:40] for key, value in reasons.items()},
        "per_run": per_run,
        "tolerances": {"injection_lag_p99_samples": LAG_P99_TOLERANCE_SAMPLES,
                       "injection_lag_max_samples": LAG_MAX_TOLERANCE_SAMPLES,
                       "valid_segment_fraction": VALID_SEGMENT_FRACTION},
        "not_claimed": ["identical received IQ, MAC allocations, HARQ, or delivered traffic across tests",
                        "strict real-time operation (see per_run real_time_ratio)"],
    }


def _segment_values(report: dict[str, Any], extract) -> dict[int, float]:
    out = {}
    for seg in report.get("segments") or []:
        if not seg.get("valid"):
            continue
        value = extract(seg)
        if value is not None:
            out[seg["k"]] = value
    return out


# Bandwidth of one PRB at 15 kHz SCS (12 subcarriers).
PRB_BANDWIDTH_HZ = 12 * 15_000.0


def cell_kpis(m: dict[str, Any]) -> dict[str, Any]:
    """Cell-level fairness, latency, resource-efficiency and reliability KPIs
    of one run, over its valid measured seconds (single_cell_analyze metrics).

    Pooled values weight each UE by its packets (delay by received packets,
    drop and PDB violation by offered packets), so a UE with little traffic
    does not count as much as a backlogged one; "worst UE" is the maximum (or,
    for throughput, the minimum) over the UEs."""

    ues = list(m["per_ue"].values())

    def get(d, *keys):
        # Reports of older analyzers lack some fields; a missing one is None.
        for key in keys:
            d = d.get(key) if isinstance(d, dict) else None
        return d

    def pooled(values_weights):
        pairs = [(v, w) for v, w in values_weights if v is not None and w]
        total = sum(w for _, w in pairs)
        return sum(v * w for v, w in pairs) / total if total else None

    def worst(values, high_is_bad=True):
        values = [v for v in values if v is not None]
        return (max(values) if high_is_bad else min(values)) if values else None

    used_prb = (get(m, "resource_utilization", "dl_prb") or 0) * (get(m, "resource_utilization", "nof_prb") or 0)
    mac_dl = get(m, "aggregate_throughput_mbps", "mac_dl")
    app_dl = get(m, "aggregate_throughput_mbps", "dl")
    # QoS classes of the `mixed` profile: delay-sensitive 5QI 7 and bulk 5QI 9
    # (None when the profile has no UE of that class).
    delay_ues = [u for u in ues if u.get("five_qi") == 7]
    bulk_ues = [u for u in ues if u.get("five_qi") == 9]
    bulk_dl = [get(u, "throughput_mbps", "dl") for u in bulk_ues]
    return {
        # throughput at the MAC: what the scheduler served, before RLC/PDCP/IP
        "mac_dl_mbps": mac_dl,
        "dl_goodput_ratio": app_dl / mac_dl if app_dl is not None and mac_dl else None,
        # QoS differentiation
        "qos7_worst_dl_delay_p99_ms": worst([get(u, "delay_radio_ms", "dl", "p99") for u in delay_ues]),
        "qos7_pdb_violation_rate": pooled([(u.get("pdb_violation_rate"), u.get("offered_packets")) for u in delay_ues]),
        "qos9_dl_mbps": sum(bulk_dl) if bulk_dl and None not in bulk_dl else None,
        "starvation_events": (sum(get(u, "starvation", "events") or 0 for u in ues)
                              if any(get(u, "starvation", "events") is not None for u in ues) else None),
        # fairness
        "worst_ue_dl_mbps": worst([get(u, "throughput_mbps", "dl") for u in ues], high_is_bad=False),
        "worst_ue_dl_wait_p99_ms": worst([get(u, "starvation", "dl_wait_ms", "p99") for u in ues]),
        # latency (radio time, application layer)
        "dl_delay_mean_ms": pooled([(get(u, "delay_radio_ms", "dl", "mean"), get(u, "delay_radio_ms", "dl", "count"))
                                    for u in ues]),
        "worst_ue_dl_delay_p95_ms": worst([get(u, "delay_radio_ms", "dl", "p95") for u in ues]),
        "worst_ue_dl_delay_p99_ms": worst([get(u, "delay_radio_ms", "dl", "p99") for u in ues]),
        # resource efficiency
        "dl_spectral_efficiency_bps_hz": (mac_dl * 1e6 / (used_prb * PRB_BANDWIDTH_HZ)
                                          if mac_dl is not None and used_prb else None),
        "dl_mcs_mean": pooled([(get(u, "link", "dl_mcs"), 1) for u in ues]),
        # link adaptation: what the gNB chose (MCS) and what it chose from
        # (reported CQI, its own PUSCH SINR estimate), UE means
        "ul_mcs_mean": pooled([(get(u, "link", "ul_mcs"), 1) for u in ues]),
        "cqi_mean": pooled([(get(u, "link", "cqi"), 1) for u in ues]),
        "pusch_snr_db_mean": pooled([(get(u, "link", "pusch_snr_db"), 1) for u in ues]),
        "worst_ue_dl_bler": worst([get(u, "link", "dl_bler") for u in ues]),
        "worst_ue_ul_bler": worst([get(u, "link", "ul_bler") for u in ues]),
        # reliability
        "dl_bler": pooled([(get(u, "link", "dl_bler"), 1) for u in ues]),
        "ul_bler": pooled([(get(u, "link", "ul_bler"), 1) for u in ues]),
        "drop_rate": pooled([(u.get("drop_rate"), u.get("offered_packets")) for u in ues]),
        "pdb_violation_rate": pooled([(u.get("pdb_violation_rate"), u.get("offered_packets")) for u in ues]),
    }


def compare(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-test summaries and the paired radio-second comparison Test 1 - Test 2
    (same k = same channel and offered load), plus the A/A control of any test
    that ran more than once."""

    by_arm: dict[str, list[dict[str, Any]]] = {}
    labels: dict[str, str] = {}
    tests: dict[str, Any] = {}
    for run in runs:
        labels[run["arm"]] = run.get("label", run["arm"])
        tests[run["arm"]] = run.get("test")
        if (run.get("report") or {}).get("metrics"):
            by_arm.setdefault(run["arm"], []).append(run["report"])
    by_arm = {arm: by_arm[arm] for arm in sorted(by_arm)}
    ue_ids = sorted({ue for reports in by_arm.values() for r in reports for ue in r["metrics"]["per_ue"]},
                    key=lambda u: int(u[2:]))

    def mean(values):
        values = [v for v in values if v is not None]
        return sum(values) / len(values) if values else None

    summaries = {}
    for arm, reports in by_arm.items():
        ms = [r["metrics"] for r in reports]
        summaries[arm] = {
            "label": labels[arm], "test": tests[arm],
            "runs": len(reports),
            "aggregate_dl_mbps": mean([m["aggregate_throughput_mbps"]["dl"] for m in ms]),
            "aggregate_ul_mbps": mean([m["aggregate_throughput_mbps"]["ul"] for m in ms]),
            "jain_dl": mean([m["jain_fairness"]["dl_throughput"] for m in ms]),
            "jain_dl_prb_share": mean([m["jain_fairness"]["dl_prb_share"] for m in ms]),
            "dl_prb_util": mean([m["resource_utilization"]["dl_prb"] for m in ms]),
            "decision_us_mean": mean([m["decision_latency_us"]["mean"] for m in ms]),
            "per_ue": {ue: {
                "role": ms[0]["per_ue"][ue]["role"], "five_qi": ms[0]["per_ue"][ue]["five_qi"],
                "dl_mbps": mean([m["per_ue"][ue]["throughput_mbps"]["dl"] for m in ms]),
                "ul_mbps": mean([m["per_ue"][ue]["throughput_mbps"]["ul"] for m in ms]),
                "dl_prb_share": mean([m["per_ue"][ue]["dl_prb_share"] for m in ms]),
                "dl_delay_radio_p95_ms": mean([m["per_ue"][ue]["delay_radio_ms"]["dl"]["p95"] for m in ms]),
                "dl_delay_radio_p99_ms": mean([m["per_ue"][ue]["delay_radio_ms"]["dl"]["p99"] for m in ms]),
                "pdb_violation_rate": mean([m["per_ue"][ue]["pdb_violation_rate"] for m in ms]),
                "drop_rate": mean([m["per_ue"][ue]["drop_rate"] for m in ms]),
                "dl_mcs": mean([m["per_ue"][ue]["link"]["dl_mcs"] for m in ms]),
                "ul_mcs": mean([m["per_ue"][ue]["link"].get("ul_mcs") for m in ms]),
                "cqi": mean([m["per_ue"][ue]["link"]["cqi"] for m in ms]),
                "dl_bler": mean([m["per_ue"][ue]["link"].get("dl_bler") for m in ms]),
                "ul_bler": mean([m["per_ue"][ue]["link"].get("ul_bler") for m in ms]),
            } for ue in ue_ids},
            "real_time_ratio": mean([(r.get("runtime") or {}).get("real_time_ratio") for r in reports]),
            # Seconds a UE was out of service (e.g. every DL HARQ failed at an
            # MCS the channel could not carry) are excluded from every other
            # KPI; this is how much of the measurement that left.
            "valid_seconds_fraction": mean([r["segments_valid"] / r["segments_complete"] for r in reports
                                            if r.get("segments_complete")]),
        }
        kpis = [cell_kpis(m) for m in ms]
        summaries[arm].update({key: mean([k[key] for k in kpis]) for key in kpis[0]})

    def averaged(reports, extract) -> dict[int, float]:
        sets = [_segment_values(r, extract) for r in reports]
        common = set.intersection(*(set(s) for s in sets)) if sets else set()
        return {k: sum(s[k] for s in sets) / len(sets) for k in sorted(common)}

    def paired(xs: dict[int, float], ys: dict[int, float]) -> dict[str, Any]:
        ks = sorted(set(xs) & set(ys))
        diffs = [xs[k] - ys[k] for k in ks]
        return {"n": len(diffs), "mean_x": mean([xs[k] for k in ks]), "mean_y": mean([ys[k] for k in ks]),
                "mean_diff": mean(diffs), "ci95": analyze.batch_means_ci(diffs) if diffs else None}

    series = {"aggregate_dl_mbps": lambda s: s["dl_mbps"], "jain_dl": lambda s: s["jain_dl"],
              "dl_prb_util": lambda s: s["dl_prb_util"], "decision_us": lambda s: s["decision_us_mean"]}
    for ue in ue_ids:
        series[f"{ue}_dl_mbps"] = lambda s, u=ue: s["ues"][u]["dl_mbps"]
        series[f"{ue}_dl_prb_share"] = lambda s, u=ue: s["ues"][u]["dl_prb_share"]
    arms = list(by_arm)
    pairs = {}
    if len(arms) == 2:
        x, y = arms
        pairs = {name: paired(averaged(by_arm[x], f), averaged(by_arm[y], f)) for name, f in series.items()}
        pairs["note"] = (f"x = {labels[x]}, y = {labels[y]}; paired by radio second k (same channel updates and "
                         "offered packets), each test averaged over its runs; 95% CI by batch means over consecutive "
                         "seconds, which are not independent -- use several seeds for a campaign-level interval")
        if json_key(tests[x]) == json_key(tests[y]):
            pairs["note"] += ". Both tests are configured identically: this comparison is an A/A control."
    aa = {}
    for arm, reports in by_arm.items():
        if len(reports) >= 2:
            aa[arm] = {name: paired(_segment_values(reports[0], f), _segment_values(reports[1], f))
                       for name, f in series.items()}
    return {"tests": summaries, "paired": pairs, "aa_control": aa,
            "aa_note": "same test, same frozen inputs, two cold runs: the differences measure the radio stack's "
                       "run-to-run nondeterminism, which a Test 1 / Test 2 difference must exceed to be meaningful"}


def write_reports(out_dir: pathlib.Path, session: dict[str, Any], runs: list[dict[str, Any]],
                  channel_bundle: pathlib.Path, traffic_bundle: pathlib.Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    fairness = audit(session, runs, channel_bundle, traffic_bundle)
    comparison = compare(runs)
    report = {"kind": "ocudu-single-cell-scheduler-benchmark-report", "session": session,
              "fairness": {k: fairness[k] for k in ("verdict", "gates", "reasons")}, "comparison": comparison,
              "runs": [{"run_id": r["run_id"], "arm": r["arm"], "label": r.get("label"),
                        "gate": (r.get("gate") or {}).get("status")}
                       for r in runs]}
    (out_dir / "fairness-audit.json").write_text(json.dumps(fairness, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / "benchmark-report.json").write_text(json.dumps(report, indent=2, sort_keys=True, default=analyze._json_default)
                                                   + "\n", encoding="utf-8")
    with (out_dir / "per-ue-kpis.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["run_id", "test", "scheduler", "ue", "role", "five_qi", "dl_mbps", "ul_mbps", "dl_prb_share",
                         "dl_delay_radio_p95_ms", "dl_delay_radio_p99_ms", "pdb_violation_rate", "drop_rate",
                         "dl_mcs", "cqi", "segments_valid", "link_adaptation", "fixed_dl_mcs", "fixed_ul_mcs",
                         "ul_mcs", "dl_bler", "ul_bler", "pusch_snr_db"])
        for run in runs:
            m = (run.get("report") or {}).get("metrics")
            if not m:
                continue
            test = run.get("test") or {}
            for ue, u in m["per_ue"].items():
                writer.writerow([run["run_id"], run["arm"], test.get("scheduler"), ue, u["role"], u["five_qi"], u["throughput_mbps"]["dl"],
                                 u["throughput_mbps"]["ul"], u["dl_prb_share"], u["delay_radio_ms"]["dl"]["p95"],
                                 u["delay_radio_ms"]["dl"]["p99"], u["pdb_violation_rate"], u["drop_rate"],
                                 u["link"]["dl_mcs"], u["link"]["cqi"], m["segments"], test.get("link_adaptation"),
                                 test.get("fixed_dl_mcs"), test.get("fixed_ul_mcs"), u["link"].get("ul_mcs"),
                                 u["link"].get("dl_bler"), u["link"].get("ul_bler"), u["link"].get("pusch_snr_db")])
    validation = {"verdict": fairness["verdict"], "gates": fairness["gates"], "reasons": fairness["reasons"],
                  "per_run": fairness["per_run"]}
    (out_dir / "validation.json").write_text(json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"fairness": fairness, "report": report}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session-root", type=pathlib.Path, required=True,
                        help="a session directory (session.json, bundle/, policy_runs/)")
    args = parser.parse_args(argv)
    import single_cell_runner
    result = single_cell_runner.finalize(args.session_root)
    print(json.dumps({"verdict": result["fairness"]["verdict"], "gates": result["fairness"]["gates"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
