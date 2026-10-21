#!/usr/bin/env python3
"""Single-cell policy run -> KPIs on the radio-time axis, plus its evidence.

Time base. The broker logs, for every applied trace update, the link sample
it took effect on and the wall time (`event=control_sample_applied`). Those
points map wall time to radio samples on each link; segment k is radio
second k after the measurement sample S0 (replay epoch file), i.e. the same
1000 slots and the same 10 channel updates in every policy run of a session,
however fast or slow each run's radios ran. Reference links: the gNB's
receive stream (ue0>gnb0) for gNB-side events (scheduler log, metrics), each
UE's own link (gnb0>ueI for DL, ueI>gnb0 for UL) for its packets.

Reads (incrementally, so the controller can poll a live run):
  logs/broker.log                 applied updates, noise epochs, counters
  logs/replay-bridge.log          replay status
  run/measure-epoch.json          S0, samples per update, updates
  logs/gnb0-internal.log          SCHED `Slot decisions` (PRBs, TBS, decision time,
                                  HARQ process of each grant) and HARQ discards
  logs/gnb-metrics.jsonl          per-UE CQI/MCS/BLER (service validity)
  logs/srsue-<ue>.log/.csv        C-RNTI, camped PCI, DL BLER (service validity)
  logs/tx-*.bin, rx-*.bin         offered and delivered packets
Metrics: METRIC_DEFINITIONS (analyze.py) plus the radio-time notes below.
"""

from __future__ import annotations

import argparse
import array
import bisect
import collections
import json
import math
import pathlib
import re
import struct
import sys
import time
from typing import Any, Sequence

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import analyze  # noqa: E402
import channel_trace  # noqa: E402
import radio_traffic  # noqa: E402
import traffic  # noqa: E402
import traffic_trace  # noqa: E402

GNB_REF_LINK = "ue0>gnb0:sionna_rt"
SLOTS_PER_RADIO_SECOND = 1000
APPLIED_LINE = re.compile(
    r"^event=control_sample_applied link_id=(\S+) tag=(\S+) seqno=(\d+) slot=(\d+) sample=(\d+) "
    r"target_sample=(\d+) count=(\d+) digest=([0-9a-f]{16}) unix_ns=(\d+)$")
NOISE_LINE = re.compile(r"^event=noise_epoch node=(\S+) sample=(\d+) applied=([01])$")
NOISE_MISSED = re.compile(r"^event=noise_epoch_missed node=(\S+) target_sample=(\d+) sample=(\d+)$")
TAG = re.compile(r"^(?:k=(\d+)|attach);sha=([0-9a-f]{16})$")
# A link with more than one antenna lane (a 2-antenna gNB) applies its whole
# matrix profile atomically and logs it once, under the key of its first lane,
# `<link>#r0t0` (src/config.cpp lane_key); the digest covers every lane
# (runtime_control.h matrix_profile_digest). Evidence is keyed by the link.
LANE_SUFFIX = re.compile(r"^(.+)#r\d+t\d+$")
VALID_SEGMENT_FRACTION = 0.9
# A UE is out of service in a second when srsUE decoded none of its PDSCH
# transport blocks (its own CRC count, `dl_bler` in percent), as the gNB-side
# rule is "every DL HARQ failed". srsUE's `dl_snr` is not used: its DMRS noise
# estimate is EPRE - |mean pilot|^2 over the grant (lib/src/phy/ch_estimation/
# dmrs_sch.c), which measures the channel's frequency selectivity, so a UE in a
# notch reads about -5 dB while it decodes QPSK at 1 % BLER. A UE that decodes
# some blocks counts as in service: a poor link adaptation shows in the KPIs
# instead of being excluded from them.
UE_OUT_OF_SERVICE_DL_BLER = 100.0
# HARQ transport blocks, followed per (UE, direction, h_id) through the
# scheduler log: a newtx opens a TB, a retx of the same process adds a
# transmission, the next newtx of the process closes it as delivered (OCUDU
# reuses an h_id only once it is empty, lib/scheduler/cell/cell_harq_manager.cpp)
# and a `Discarding ... HARQ` line closes it as lost. A TB counts in the radio
# second of its first transmission; TBs still open at the end are not counted.
HARQ_GRANT = re.compile(r"\b(DL|UL): ue=\d+ (?:c-)?rnti=(0x[0-9a-f]+) h_id=(\d+) .*?newtx=(\w+) rv=\d+ tbs=\d+")
HARQ_DISCARD = re.compile(r"rnti=(0x[0-9a-f]+) h_id=(\d+): Discarding (DL|UL) HARQ(?: process TB with tbs=\d+)?\. "
                          r"Cause: (Maximum number of reTxs|The scheduler took too long|Timeout was reached|"
                          r"Retxs for this HARQ process were cancelled)")
HARQ_DISCARD_CAUSES = {"Maximum number of reTxs": "max_retx", "The scheduler took too long": "retx_timeout",
                       "Timeout was reached": "feedback_timeout",
                       "Retxs for this HARQ process were cancelled": "cancelled"}

RADIO_TIME_DEFINITIONS = {
    "radio_time": "Segment k = radio second k after measurement sample S0: samples [S0 + k*fs, S0 + (k+1)*fs) of "
                  "the link concerned. Identical channel updates and offered packets fall in it in every run.",
    "throughput_radio": "Delivered UDP payload per radio second (bytes arriving in the segment, mapped through the "
                        "UE's own link clock). A slow run delivers the same radio-time throughput in more wall time.",
    "delay_radio": "Radio-time delay: (link sample at arrival - packet's target sample) / fs. The wall-clock delay "
                   "(receiver minus sender host clock) is also reported; it scales with the run's radio speed.",
    "pdb_violation": "Share of offered packets whose radio-time delay exceeds the UE's 5QI PDB, or never arrived.",
    "prb_utilization": "Granted PRBs (UE + common) / (106 PRBs x 1000 slots) per radio second.",
    "real_time_ratio": "Radio samples per wall second / 23.04e6 on the gNB receive link (1.0 = real time).",
    "harq_first_tx_bler": "Share of HARQ transport blocks not delivered by their first transmission (retransmitted "
                          "or discarded), from the scheduler log. The gNB-metrics BLER counts every transmission.",
    "harq_residual_bler": "Share of HARQ transport blocks the gNB discarded (maximum retransmissions, retransmission "
                          "or feedback timeout): what HARQ did not recover and RLC or the application must.",
    "harq_recovery_rate": "Share of the transport blocks that failed their first transmission which a retransmission "
                          "then delivered.",
    "harq_tx_per_tb":"Transmissions per HARQ transport block, first one included (1.0 = no retransmission).",
    "harq_rtt": "Radio time between consecutive transmissions of one transport block (HARQ round trip plus "
                "the scheduler's retransmission wait).",
}


class LinkClock:
    """Piecewise-linear wall <-> sample map of one link, from applied updates."""

    def __init__(self) -> None:
        self.samples: list[int] = []
        self.wall: list[int] = []

    def add(self, sample: int, unix_ns: int) -> None:
        if self.samples and sample <= self.samples[-1]:
            return
        self.samples.append(sample)
        self.wall.append(unix_ns)

    def ready(self) -> bool:
        return len(self.samples) >= 2

    def sample_at(self, unix_ns: int) -> float | None:
        if not self.ready():
            return None
        i = bisect.bisect_left(self.wall, unix_ns)
        i = min(max(i, 1), len(self.wall) - 1)
        w0, w1, s0, s1 = self.wall[i - 1], self.wall[i], self.samples[i - 1], self.samples[i]
        return s0 + (s1 - s0) * (unix_ns - w0) / (w1 - w0) if w1 != w0 else float(s1)

    def wall_at(self, sample: float) -> float | None:
        if not self.ready():
            return None
        i = bisect.bisect_left(self.samples, sample)
        i = min(max(i, 1), len(self.samples) - 1)
        w0, w1, s0, s1 = self.wall[i - 1], self.wall[i], self.samples[i - 1], self.samples[i]
        return w0 + (w1 - w0) * (sample - s0) / (s1 - s0) if s1 != s0 else float(w1)

    def speed(self) -> float | None:
        """Radio samples per wall second over the mapped span, / 23.04e6."""

        if not self.ready() or self.wall[-1] == self.wall[0]:
            return None
        return (self.samples[-1] - self.samples[0]) / ((self.wall[-1] - self.wall[0]) / 1e9) / 23.04e6


class FlowState:
    def __init__(self, meta: dict[str, Any], times: list[float]) -> None:
        self.meta = meta
        self.times = times                      # offered radio offset (s) by seq
        self.target: dict[int, int] = {}       # seq -> target sample (sender log)
        self.sent_lag: dict[int, int] = {}     # seq -> observed - target
        self.tx_ns: dict[int, int] = {}
        self.rx: dict[int, tuple[int, int]] = {}  # seq -> (tx_ns, rx_ns)
        self.duplicates = 0


class SingleCellRun:
    def __init__(self, run_root: pathlib.Path, channel_bundle: pathlib.Path, traffic_bundle: pathlib.Path,
                 warmup_seconds: int = 0) -> None:
        self.run_root = run_root
        self.warmup_seconds = warmup_seconds
        self.log_dir = run_root / "logs"
        self.config_dir = run_root / "configs"
        self.benchmark = json.loads((self.config_dir / "benchmark.json").read_text(encoding="utf-8"))
        self.ue_ids = [ue["device_id"] for ue in self.benchmark["ues"]]
        self.ue_meta = {ue["device_id"]: ue for ue in self.benchmark["ues"]}
        self.channel_manifest = json.loads((channel_bundle / channel_trace.MANIFEST_FILE).read_text(encoding="utf-8"))
        self.traffic_manifest = json.loads((traffic_bundle / traffic_trace.MANIFEST_FILE).read_text(encoding="utf-8"))
        self.channel_bundle = channel_bundle
        self.traffic_bundle = traffic_bundle
        self.updates = int(self.channel_manifest["config"]["updates"])
        self.period = int(self.channel_manifest["config"]["samples_per_update"])
        self.fs = float(self.channel_manifest["config"]["sample_rate_hz"])
        self.measure_seconds = int(self.updates * self.period // int(self.fs))
        self.readers: dict[str, analyze.TailReader] = {}
        self.epoch: dict[str, Any] | None = None
        self.clocks: dict[str, LinkClock] = collections.defaultdict(LinkClock)
        self.applied: dict[str, dict[Any, dict[str, Any]]] = collections.defaultdict(dict)  # link -> k|"attach"
        self.duplicate_applies = 0
        self.malformed_evidence = 0
        self.noise_epochs: dict[str, dict[str, int]] = {}
        self.noise_missed: list[dict[str, Any]] = []
        self.broker_stop: dict[str, int] = {}
        self.sched = analyze.CellSched()
        self.rnti: dict[str, str] = {}
        self.harq_open: dict[tuple[str, str, int], dict[str, Any]] = {}  # (ue, dl|ul, h_id) -> open TB
        self.harq_seg: dict[int, dict[str, dict[str, dict[str, Any]]]] = {}  # k -> ue -> dl|ul -> closed TBs
        self.gnb_metrics: dict[int, dict[str, list[dict[str, Any]]]] = {}
        self.cell_metrics: dict[int, list[dict[str, Any]]] = {}
        self.ue_side: dict[str, dict[int, dict[str, float]]] = {ue: {} for ue in self.ue_ids}
        self.ue_start_ms: dict[str, int] = {}
        self.pending_sched: list[str] = []
        self.pending_metrics: list[dict[str, Any]] = []
        self.flows: dict[int, FlowState] = {}
        times: dict[int, list[float]] = collections.defaultdict(list)
        for t, seq, fid, _, _, _ in traffic_trace.iter_records(traffic_bundle):
            times[fid].append((seq, t))
        for flow in self.traffic_manifest["flows"]:
            ordered = [t for _, t in sorted(times.get(flow["flow_id"], []))]
            self.flows[flow["flow_id"]] = FlowState(flow, ordered)
        self.sender_reports: dict[str, Any] = {}
        self.replay_events: list[dict[str, Any]] = []

    def reader(self, name: str, **kwargs: Any) -> analyze.TailReader:
        if name not in self.readers:
            self.readers[name] = analyze.TailReader(self.log_dir / name, **kwargs)
        return self.readers[name]

    # --- ingestion -------------------------------------------------------------
    def poll(self) -> None:
        if self.epoch is None:
            path = self.run_root / "run" / "measure-epoch.json"
            if path.exists():
                try:
                    self.epoch = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    self.epoch = None
        self.poll_broker()
        self.poll_replay()
        self.poll_rnti()
        self.poll_sched()
        self.poll_gnb_metrics()
        self.poll_ue_metrics()
        self.poll_traffic()

    def poll_broker(self) -> None:
        for line in self.reader("broker.log").lines():
            if line.startswith("event=control_sample_applied "):
                match = APPLIED_LINE.match(line)
                if not match:
                    self.malformed_evidence += 1
                    continue
                link_id, tag, seqno, slot, sample, target, count, digest, unix_ns = match.groups()
                tag_match = TAG.match(tag)
                if not tag_match:
                    self.malformed_evidence += 1
                    continue
                key: Any = "attach" if tag_match.group(1) is None else int(tag_match.group(1))
                entry = {"tag": tag, "sha16": tag_match.group(2), "seqno": int(seqno), "slot": int(slot),
                         "sample": int(sample), "target_sample": int(target), "count": int(count),
                         "digest": digest, "unix_ns": int(unix_ns)}
                lane_match = LANE_SUFFIX.match(link_id)
                if lane_match:
                    link_id = lane_match.group(1)
                if key in self.applied[link_id]:
                    self.duplicate_applies += 1
                self.applied[link_id][key] = entry
                if key != "attach":
                    self.clocks[link_id].add(int(sample), int(unix_ns))
            elif line.startswith("event=noise_epoch "):
                match = NOISE_LINE.match(line)
                if match:
                    self.noise_epochs[match.group(1)] = {"sample": int(match.group(2)), "applied": int(match.group(3))}
            elif line.startswith("event=noise_epoch_missed "):
                match = NOISE_MISSED.match(line)
                if match:
                    self.noise_missed.append({"node": match.group(1), "target_sample": int(match.group(2)),
                                              "sample": int(match.group(3))})
            elif line.startswith("event=stop "):
                self.broker_stop = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", line)}

    def poll_replay(self) -> None:
        for line in self.reader("replay-bridge.log").lines():
            if line.startswith("{"):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("event", "").startswith("replay_"):
                    self.replay_events.append({k: v for k, v in event.items() if k not in ("links", "errors")}
                                              | ({"errors": event["errors"]} if "errors" in event else {}))

    def poll_rnti(self) -> None:
        for ue in self.ue_ids:
            for line in self.reader(f"srsue-{ue}.log").lines():
                match = analyze.RNTI_LINE.search(line)
                if match:
                    self.rnti[match.group(1)] = ue
            start = self.log_dir / f"srsue-{ue}.start_unix_ms"
            if ue not in self.ue_start_ms and start.exists():
                try:
                    self.ue_start_ms[ue] = int(start.read_text().strip())
                except ValueError:
                    pass

    def gnb_segment(self, unix_ns: float) -> int | None:
        """Radio second of a gNB-side wall instant, or None outside the measurement."""

        if self.epoch is None:
            return None
        clock = self.clocks.get(GNB_REF_LINK)
        sample = clock.sample_at(int(unix_ns)) if clock else None
        if sample is None:
            return None
        k = math.floor((sample - self.epoch["s0"]) / self.fs)
        return k if 0 <= k < self.measure_seconds else None

    def poll_sched(self) -> None:
        # Lines before the clock exists are kept until it does, so none is misfiled.
        lines = self.pending_sched + self.reader("gnb0-internal.log").lines()
        self.pending_sched = []
        clock_ready = self.epoch is not None and self.clocks[GNB_REF_LINK].ready()
        state = self.sched
        for line in lines:
            if "Slot decisions" not in line:
                if "nof_crbs:" in line:
                    match = analyze.NOF_CRBS.search(line)
                    if match:
                        state.nof_prb = int(match.group(1))
                elif "Discarding" in line:
                    match = HARQ_DISCARD.search(line)
                    if match and not clock_ready:
                        self.pending_sched.append(line)  # in order with the grants it closes
                    elif match and match.group(1) in self.rnti:
                        self._harq_close((self.rnti[match.group(1)], match.group(3).lower(), int(match.group(2))),
                                         HARQ_DISCARD_CAUSES[match.group(4)])
                continue
            match = analyze.SLOT_LINE.match(line)
            if not match:
                continue
            wall_ns = analyze.parse_log_time_ns(match.group(1))
            if not clock_ready:
                # Before the measurement only the grant history matters; keep
                # the last few seconds so the first in-window gap is right.
                self.pending_sched.append(line)
                continue
            state.lines += 1
            abs_slot = state.abs_slot(int(match.group(2)), int(match.group(3)))
            k = self.gnb_segment(wall_ns)
            entries = match.group(6)
            decision_us = float(match.group(5))
            state.recent.append((
                abs_slot, wall_ns, decision_us,
                [(self.rnti.get(m.group(2), "other"), int(m.group(3)), int(m.group(4)), m.group(5) == "true")
                 for m in analyze.DL_ENTRY.finditer(entries)]
                + [("common", int(m.group(1)), int(m.group(2)), True) for m in analyze.COMMON_ENTRY.finditer(entries)],
                [(self.rnti.get(m.group(2), "other"), int(m.group(3)), int(m.group(4)), m.group(5) == "true")
                 for m in analyze.UL_ENTRY.finditer(entries)],
            ))
            if k is None:
                self._track(state, None, abs_slot, entries)
                continue
            seg = state.segment(k)
            seg["decision_us"].append(decision_us)
            state.decision_us_all.append(decision_us)
            seg["first_slot"] = abs_slot if seg["first_slot"] is None else min(seg["first_slot"], abs_slot)
            seg["last_slot"] = abs_slot if seg["last_slot"] is None else max(seg["last_slot"], abs_slot)
            seg["logged_slots"] = seg.get("logged_slots", 0) + 1
            for common in analyze.COMMON_ENTRY.finditer(entries):
                seg["dl_common_prb"] += int(common.group(2)) - int(common.group(1))
            self._track(state, k, abs_slot, entries)
        if len(self.pending_sched) > 20000:
            self.pending_sched = self.pending_sched[-20000:]

    def _track(self, state: analyze.CellSched, k: int | None, abs_slot: int, entries: str) -> None:
        for dl in analyze.DL_ENTRY.finditer(entries):
            ue = self.rnti.get(dl.group(2))
            if ue is None:
                continue
            prbs, newtx, tbs = int(dl.group(4)) - int(dl.group(3)), dl.group(5) == "true", int(dl.group(6))
            if k is not None:
                useg = state.ue_segment(k, ue)
                state.segment(k)["dl_prb"] += prbs
                useg["dl_prb"] += prbs
                useg["dl_grants"] += 1
                useg["dl_newtx_bytes" if newtx else "dl_retx_bytes"] += tbs
            if newtx:
                previous = state.last_dl_newtx.get(ue)
                if previous is not None and previous[1] > 0 and k is not None:
                    wait = (abs_slot - previous[0]) * 1000.0 / analyze.SLOTS_PER_SECOND
                    if wait >= 0:
                        state.ue_segment(k, ue)["dl_wait_ms"].append(wait)
                state.last_dl_newtx[ue] = (abs_slot, int(dl.group(7)) if dl.group(7) is not None else 0)
        for ul in analyze.UL_ENTRY.finditer(entries):
            ue = self.rnti.get(ul.group(2))
            if ue is None:
                continue
            prbs, newtx, tbs = int(ul.group(4)) - int(ul.group(3)), ul.group(5) == "true", int(ul.group(6))
            if k is not None:
                useg = state.ue_segment(k, ue)
                state.segment(k)["ul_prb"] += prbs
                useg["ul_prb"] += prbs
                useg["ul_grants"] += 1
                useg["ul_newtx_bytes" if newtx else "ul_retx_bytes"] += tbs
        for grant in HARQ_GRANT.finditer(entries):
            ue = self.rnti.get(grant.group(2))
            if ue is None:
                continue
            key = (ue, grant.group(1).lower(), int(grant.group(3)))
            if grant.group(4) == "true":
                self._harq_close(key)
                self.harq_open[key] = {"k": k, "tx": 1, "last_slot": abs_slot, "rtt_ms": []}
            elif key in self.harq_open:
                tb = self.harq_open[key]
                tb["rtt_ms"].append((abs_slot - tb["last_slot"]) * 1000.0 / analyze.SLOTS_PER_SECOND)
                tb["tx"] += 1
                tb["last_slot"] = abs_slot

    def _harq_close(self, key: tuple[str, str, int], discard_cause: str | None = None) -> None:
        tb = self.harq_open.pop(key, None)
        if tb is None or tb["k"] is None:
            return
        ue, direction, _ = key
        out = self.harq_seg.setdefault(tb["k"], {}).setdefault(ue, {}).setdefault(
            direction, {"tbs": 0, "first_tx_ok": 0, "tx": 0, "discards": collections.Counter(), "rtt_ms": []})
        out["tbs"] += 1
        out["tx"] += tb["tx"]
        out["rtt_ms"].extend(tb["rtt_ms"])
        if discard_cause is not None:
            out["discards"][discard_cause] += 1
        elif tb["tx"] == 1:
            out["first_tx_ok"] += 1

    def harq_metrics(self, ue: str, direction: str, valid: list[int], retx_bytes: int, newtx_bytes: int) -> dict[str, Any]:
        rows = [((self.harq_seg.get(k) or {}).get(ue) or {}).get(direction) for k in valid]
        rows = [r for r in rows if r]
        tbs = sum(r["tbs"] for r in rows)
        discards: collections.Counter = sum((r["discards"] for r in rows), collections.Counter())
        lost = sum(discards.values())
        first_fail = tbs - sum(r["first_tx_ok"] for r in rows)
        return {"tbs": tbs,
                "first_tx_bler": first_fail / tbs if tbs else None,
                "residual_bler": lost / tbs if tbs else None,
                "recovery_rate": (first_fail - lost) / first_fail if first_fail else None,
                "tx_per_tb": sum(r["tx"] for r in rows) / tbs if tbs else None,
                "retx_byte_share": retx_bytes / (retx_bytes + newtx_bytes) if retx_bytes + newtx_bytes else None,
                "rtt_ms": analyze.summary([x for r in rows for x in r["rtt_ms"]]),
                "discards": {cause: discards.get(cause, 0) for cause in HARQ_DISCARD_CAUSES.values()}}

    def poll_gnb_metrics(self) -> None:
        records = self.pending_metrics
        self.pending_metrics = []
        for line in self.reader("gnb-metrics.jsonl").lines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        ready = self.epoch is not None and self.clocks[GNB_REF_LINK].ready()
        for record in records:
            if not ready:
                self.pending_metrics.append(record)
                continue
            k = self.gnb_segment(record.get("rx_unix_ms", 0) * 1_000_000)
            if k is None:
                continue
            for cell_entry in (record.get("payload") or {}).get("cells") or []:
                if "cell_metrics" in cell_entry:
                    self.cell_metrics.setdefault(k, []).append(cell_entry["cell_metrics"])
                for row in cell_entry.get("ue_list") or []:
                    rnti = row.get("rnti")
                    ue = self.rnti.get(f"0x{int(rnti):x}" if isinstance(rnti, int) else str(rnti))
                    if ue is not None:
                        self.gnb_metrics.setdefault(k, {}).setdefault(ue, []).append(row)
        self.pending_metrics = self.pending_metrics[-5000:]

    def poll_ue_metrics(self) -> None:
        if self.epoch is None or not self.clocks[GNB_REF_LINK].ready():
            return
        for ue in self.ue_ids:
            start = self.ue_start_ms.get(ue)
            reader = self.reader(f"srsue-metrics-{ue}.csv")
            for line in reader.lines():
                if line.startswith("time;"):
                    reader.header = line.split(";")  # type: ignore[attr-defined]
                    continue
                header = getattr(reader, "header", None)
                if not line or header is None or start is None:
                    continue
                fields = line.split(";")
                if len(fields) != len(header):
                    continue
                row = dict(zip(header, fields))
                try:
                    k = self.gnb_segment((start + float(row["time"])) * 1e6)
                    if k is not None:
                        self.ue_side[ue][k] = {"pci": float(row["pci"]), "dl_snr": float(row["dl_snr"]),
                                               "dl_mcs": float(row["dl_mcs"]), "dl_bler": float(row["dl_bler"])}
                except (KeyError, ValueError):
                    continue

    def poll_traffic(self) -> None:
        for name in ["tx-dl.bin"] + [f"tx-ul-{ue}.bin" for ue in self.ue_ids]:
            data = self.reader(name, binary=True, record_size=radio_traffic.SENT.size).read()
            for fid, seq, target, observed, tx_ns in radio_traffic.SENT.iter_unpack(data):
                flow = self.flows.get(fid)
                if flow is not None:
                    flow.target[seq] = target
                    flow.sent_lag[seq] = observed - target
                    flow.tx_ns[seq] = tx_ns
        for name in [f"rx-dl-{ue}.bin" for ue in self.ue_ids] + ["rx-ul.bin"]:
            data = self.reader(name, binary=True, record_size=traffic.RECORD.size).read()
            for fid, _, seq, tx_ns, rx_ns in traffic.RECORD.iter_unpack(data):
                flow = self.flows.get(fid)
                if flow is None:
                    continue
                if seq in flow.rx:
                    flow.duplicates += 1
                    continue
                flow.rx[seq] = (tx_ns, rx_ns)
        for name in ["traffic-dl-send.log"] + [f"traffic-ul-send-{ue}.log" for ue in self.ue_ids]:
            path = self.log_dir / name
            if path.exists() and name not in self.sender_reports:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if '"radio_traffic_sender_done"' in line:
                        try:
                            self.sender_reports[name] = json.loads(line)
                        except json.JSONDecodeError:
                            pass

    # --- metrics -----------------------------------------------------------------
    def flow_link(self, flow: FlowState) -> str:
        i = flow.meta["ue_index"]
        return f"gnb0>ue{i}:sionna_rt" if flow.meta["direction"] == "dl" else f"ue{i}>gnb0:sionna_rt"

    def flow_series(self, flow: FlowState) -> dict[str, Any]:
        """Per segment: offered, delivered, late (radio PDB), rx bytes; delays."""

        s0 = self.epoch["s0"]
        clock = self.clocks.get(self.flow_link(flow))
        pdb = flow.meta["pdb_ms"]
        out = {"offered": collections.Counter(), "received": collections.Counter(), "late": collections.Counter(),
               "rx_bytes": collections.Counter(), "delay_radio_ms": collections.defaultdict(list),
               "delay_wall_ms": collections.defaultdict(list)}
        size = flow.meta["size_bytes"]
        for seq, t in enumerate(flow.times):
            k = math.floor(t)
            if k >= self.measure_seconds:
                continue
            out["offered"][k] += 1
            got = flow.rx.get(seq)
            if got is None:
                continue
            tx_ns, rx_ns = got
            out["received"][k] += 1
            out["delay_wall_ms"][k].append((rx_ns - tx_ns) / 1e6)
            arrival = clock.sample_at(rx_ns) if clock else None
            if arrival is not None:
                target = flow.target.get(seq, s0 + round(t * self.fs))
                delay = (arrival - target) / self.fs * 1000.0
                out["delay_radio_ms"][k].append(delay)
                if delay > pdb:
                    out["late"][k] += 1
                k_rx = math.floor((arrival - s0) / self.fs)
                if 0 <= k_rx < self.measure_seconds:
                    out["rx_bytes"][k_rx] += size
        return out

    def segment_validity(self, k: int) -> dict[str, Any]:
        reasons = []
        for ue in self.ue_ids:
            if any(self.gnb_metrics.values()):
                rows = [row for j in (k, k - 1, k + 1) for row in (self.gnb_metrics.get(j) or {}).get(ue, [])]
                if not rows:
                    reasons.append(f"{ue}: not in the gNB report")
                elif all(row.get("dl_nof_ok", 0) == 0 and row.get("dl_nof_nok", 0) > 0 for row in rows):
                    reasons.append(f"{ue}: every DL HARQ failed")
            if self.ue_side[ue]:
                side = self.ue_side[ue].get(k) or self.ue_side[ue].get(k - 1) or self.ue_side[ue].get(k + 1)
                if side is None:
                    reasons.append(f"{ue}: no UE metrics row")
                elif int(side["pci"]) != self.benchmark["gnb"]["pci"]:
                    reasons.append(f"{ue}: out of service (camped on pci={int(side['pci'])})")
                elif side["dl_bler"] >= UE_OUT_OF_SERVICE_DL_BLER:
                    reasons.append(f"{ue}: nothing decoded (srsUE DL BLER {side['dl_bler']:.0f}%)")
        seg = self.sched.seg.get(k)
        if seg is None or seg["first_slot"] is None:
            reasons.append("no scheduler activity")
        return {"valid": not reasons, "reasons": reasons}

    def complete_segments(self, live: bool) -> int:
        if self.epoch is None:
            return 0
        clock = self.clocks.get(GNB_REF_LINK)
        if clock is None or not clock.samples:
            return 0
        reached = (clock.samples[-1] - self.epoch["s0"]) / self.fs
        if not live:
            reached = self.measure_seconds if reached >= self.measure_seconds - 0.1 else reached
        return max(0, min(self.measure_seconds, math.floor(reached) - (1 if live else 0)))

    def report(self, live: bool = False) -> dict[str, Any]:
        complete = self.complete_segments(live)
        series = {fid: self.flow_series(flow) for fid, flow in self.flows.items()} if self.epoch else {}
        segments = []
        for k in range(complete):
            validity = self.segment_validity(k)
            if k < self.warmup_seconds:
                validity = {"valid": False, "reasons": ["warm-up (excluded from KPIs)"], "warmup": True}
            entry = {"k": k, **validity, "ues": {}}
            seg = self.sched.seg.get(k) or {}
            total_dl_prb = sum((seg.get("ue") or {}).get(u, {}).get("dl_prb", 0) for u in self.ue_ids)
            for ue in self.ue_ids:
                useg = (seg.get("ue") or {}).get(ue) or {}
                fids = [fid for fid, f in self.flows.items() if f.meta["ue"] == ue]
                entry["ues"][ue] = {
                    "dl_mbps": sum(series[f]["rx_bytes"][k] for f in fids if self.flows[f].meta["direction"] == "dl") * 8 / 1e6,
                    "ul_mbps": sum(series[f]["rx_bytes"][k] for f in fids if self.flows[f].meta["direction"] == "ul") * 8 / 1e6,
                    "dl_prb": useg.get("dl_prb", 0),
                    "dl_prb_share": useg.get("dl_prb", 0) / total_dl_prb if total_dl_prb else None,
                    "dl_newtx_mbps": useg.get("dl_newtx_bytes", 0) * 8 / 1e6,
                }
            dl = [entry["ues"][u]["dl_mbps"] for u in self.ue_ids]
            entry["dl_mbps"] = sum(dl)
            entry["jain_dl"] = analyze.jain(dl)
            entry["dl_prb_util"] = (seg.get("dl_prb", 0) + seg.get("dl_common_prb", 0)) / (
                SLOTS_PER_RADIO_SECOND * self.sched.nof_prb)
            entry["decision_us_mean"] = (sum(seg["decision_us"]) / len(seg["decision_us"])) if seg.get("decision_us") else None
            entry["logged_slots"] = seg.get("logged_slots", 0)
            segments.append(entry)
        valid = [s["k"] for s in segments if s["valid"]]
        return {
            "kind": "ocudu-single-cell-run-report",
            "generated_unix_ms": int(time.time() * 1000),
            "live": live,
            "policy": self.benchmark["policy"],
            "link_adaptation": self.benchmark.get("link_adaptation"),
            "ue_count": len(self.ue_ids),
            "seed": self.benchmark["seed"],
            "radio_seconds_total": self.measure_seconds,
            "warmup_seconds": self.warmup_seconds,
            "measure_seconds": self.measure_seconds - self.warmup_seconds,
            "segments_complete": max(0, complete - self.warmup_seconds),
            "segments_valid": len(valid),
            "epoch": self.epoch,
            "definitions": {**analyze.METRIC_DEFINITIONS, **RADIO_TIME_DEFINITIONS},
            "metrics": self.metrics(valid, series) if self.epoch else None,
            "segments": segments,
            "evidence": self.evidence(),
            "runtime": self.runtime(),
        }

    def metrics(self, valid: list[int], series: dict[int, dict[str, Any]]) -> dict[str, Any]:
        n = len(valid)
        per_ue = {}
        dl_tput = []
        seg_ue = lambda k, ue: ((self.sched.seg.get(k) or {}).get("ue") or {}).get(ue) or {}  # noqa: E731
        total_dl_prb = sum(seg_ue(k, u).get("dl_prb", 0) for k in valid for u in self.ue_ids)
        for ue in self.ue_ids:
            meta = self.ue_meta[ue]
            rx = {"dl": 0.0, "ul": 0.0}
            offered = {"dl": 0.0, "ul": 0.0}
            delays_radio = {"dl": [], "ul": []}
            delays_wall = {"dl": [], "ul": []}
            sent = received = late = 0
            flows_out = {}
            for fid, flow in self.flows.items():
                if flow.meta["ue"] != ue:
                    continue
                s = series[fid]
                d = flow.meta["direction"]
                f_off = sum(s["offered"][k] for k in valid)
                f_rcv = sum(s["received"][k] for k in valid)
                f_late = sum(s["late"][k] for k in valid)
                f_bytes = sum(s["rx_bytes"][k] for k in valid)
                f_dr = [x for k in valid for x in s["delay_radio_ms"][k]]
                f_dw = [x for k in valid for x in s["delay_wall_ms"][k]]
                rx[d] += f_bytes * 8 / 1e6 / n if n else 0.0
                offered[d] += flow.meta["offered_mbps"]
                delays_radio[d].extend(f_dr)
                delays_wall[d].extend(f_dw)
                sent += f_off
                received += f_rcv
                late += f_late + (f_off - f_rcv)
                flows_out[flow.meta["name"]] = {
                    "direction": d, "offered_mbps": flow.meta["offered_mbps"],
                    "throughput_mbps": f_bytes * 8 / 1e6 / n if n else None,
                    "offered_packets": f_off, "received": f_rcv,
                    "drop_rate": (f_off - f_rcv) / f_off if f_off else None,
                    "pdb_violation_rate": (f_late + f_off - f_rcv) / f_off if f_off else None,
                    "delay_radio_ms": analyze.summary(f_dr), "delay_wall_ms": analyze.summary(f_dw),
                }
            useg = [seg_ue(k, ue) for k in valid]
            waits = [w for u in useg for w in u.get("dl_wait_ms", [])]
            gm = [row for k in valid for row in (self.gnb_metrics.get(k) or {}).get(ue, [])]
            dl_ok, dl_nok = sum(r.get("dl_nof_ok", 0) for r in gm), sum(r.get("dl_nof_nok", 0) for r in gm)
            ul_ok, ul_nok = sum(r.get("ul_nof_ok", 0) for r in gm), sum(r.get("ul_nof_nok", 0) for r in gm)
            ue_dl_prb = sum(u.get("dl_prb", 0) for u in useg)
            per_ue[ue] = {
                "role": meta["role"], "five_qi": meta["five_qi"], "pdb_ms": meta["qos"]["pdb_ms"],
                "throughput_mbps": rx, "offered_mbps": offered,
                "mac_newtx_mbps": {"dl": sum(u.get("dl_newtx_bytes", 0) for u in useg) * 8 / 1e6 / n if n else None,
                                   "ul": sum(u.get("ul_newtx_bytes", 0) for u in useg) * 8 / 1e6 / n if n else None},
                "delay_radio_ms": {d: analyze.summary(v) for d, v in delays_radio.items()},
                "delay_wall_ms": {d: analyze.summary(v) for d, v in delays_wall.items()},
                "offered_packets": sent, "received": received,
                "drop_rate": (sent - received) / sent if sent else None,
                "pdb_violation_rate": late / sent if sent else None,
                "dl_prb_share": ue_dl_prb / total_dl_prb if total_dl_prb else None,
                "starvation": {"dl_wait_ms": analyze.summary(waits),
                               "events": sum(1 for w in waits if w > analyze.STARVATION_THRESHOLD_MS)},
                "link": {"cqi": analyze._mean_valid([r.get("cqi") for r in gm], lambda v: v >= 0),
                         "dl_mcs": analyze._mean_valid([r.get("dl_mcs") for r in gm]),
                         "ul_mcs": analyze._mean_valid([r.get("ul_mcs") for r in gm]),
                         "dl_bler": dl_nok / (dl_ok + dl_nok) if dl_ok + dl_nok else None,
                         "ul_bler": ul_nok / (ul_ok + ul_nok) if ul_ok + ul_nok else None,
                         "pusch_snr_db": analyze._mean_valid([r.get("pusch_snr_db") for r in gm],
                                                             lambda v: -99.9 < v < 99.9)},
                "harq": {d: self.harq_metrics(ue, d, valid, sum(u.get(f"{d}_retx_bytes", 0) for u in useg),
                                              sum(u.get(f"{d}_newtx_bytes", 0) for u in useg)) for d in ("dl", "ul")},
                "flows": flows_out,
            }
            dl_tput.append(rx["dl"])
        segs = [self.sched.seg.get(k) or {} for k in valid]
        capacity = n * SLOTS_PER_RADIO_SECOND * self.sched.nof_prb
        decision = [d for s in segs for d in s.get("decision_us", [])]
        cm = [row for k in valid for row in self.cell_metrics.get(k, [])]
        return {
            "segments": n,
            "aggregate_throughput_mbps": {"dl": sum(dl_tput) if n else None,
                                          "ul": sum(per_ue[u]["throughput_mbps"]["ul"] for u in self.ue_ids) if n else None,
                                          "mac_dl": sum(u.get("dl_newtx_bytes", 0) for s in segs for u in (s.get("ue") or {}).values()) * 8 / 1e6 / n if n else None},
            "jain_fairness": {"dl_throughput": analyze.jain(dl_tput) if n else None,
                              "dl_prb_share": analyze.jain([per_ue[u]["dl_prb_share"] for u in self.ue_ids
                                                            if per_ue[u]["dl_prb_share"] is not None]) if n else None},
            "resource_utilization": {
                "dl_prb": sum(s.get("dl_prb", 0) + s.get("dl_common_prb", 0) for s in segs) / capacity if capacity else None,
                "ul_prb": sum(s.get("ul_prb", 0) for s in segs) / capacity if capacity else None,
                "nof_prb": self.sched.nof_prb},
            "decision_latency_us": analyze.summary(decision),
            "cell_metrics": {"late_dl_harqs": sum(r.get("late_dl_harqs", 0) for r in cm)},
            "per_ue": per_ue,
        }

    def evidence(self) -> dict[str, Any]:
        """What this run applied and offered, for validation.py to compare across runs."""

        links = sorted(self.applied)
        applied = {}
        for link_id in links:
            rows = self.applied[link_id]
            ks = sorted(k for k in rows if k != "attach")
            applied[link_id] = {
                "attach": rows.get("attach"),
                "updates": len(ks),
                "k_first": ks[0] if ks else None, "k_last": ks[-1] if ks else None,
                "missing": [k for k in range(self.updates) if k not in rows][:20],
                "missing_count": sum(1 for k in range(self.updates) if k not in rows),
                "exact": sum(1 for k in ks if rows[k]["sample"] == rows[k]["target_sample"]),
                "off_target": [{"k": k, "sample": rows[k]["sample"], "target": rows[k]["target_sample"]}
                               for k in ks if rows[k]["sample"] != rows[k]["target_sample"]][:20],
                "relative_ok": sum(1 for k in ks if self.epoch and
                                   rows[k]["target_sample"] == self.epoch["s0"] + k * self.period),
                "monotonic": all(rows[a]["sample"] < rows[b]["sample"] for a, b in zip(ks, ks[1:])),
                "digests": {str(k): rows[k]["digest"] for k in ks},
                "sha16": {str(k): rows[k]["sha16"] for k in ks},
            }
        lags = [lag for flow in self.flows.values() for lag in flow.sent_lag.values()]
        lags.sort()
        expected = sum(sum(1 for t in flow.times if t < self.measure_seconds) for flow in self.flows.values())
        offered_keys = sorted((fid, seq, flow.target[seq] - (self.epoch or {}).get("s0", 0))
                              for fid, flow in self.flows.items() for seq in flow.target)
        import hashlib
        offered_digest = hashlib.sha256(struct.pack(f"<{len(offered_keys) * 3}q",
                                                    *[v for key in offered_keys for v in key])).hexdigest() \
            if offered_keys else None
        return {
            "applied": applied,
            "duplicate_applies": self.duplicate_applies,
            "malformed_evidence_lines": self.malformed_evidence,
            "noise_epochs": self.noise_epochs,
            "noise_missed": self.noise_missed,
            "traffic": {
                "expected_packets": expected,
                "sent_packets": sum(len(f.target) for f in self.flows.values()),
                "offered_relative_sha256": offered_digest,
                "lag_samples": {"count": len(lags), "p50": lags[len(lags) // 2] if lags else None,
                                "p99": lags[min(len(lags) - 1, int(0.99 * len(lags)))] if lags else None,
                                "max": lags[-1] if lags else None},
                "sender_reports": self.sender_reports,
            },
            "replay_events": self.replay_events[-30:],
            "broker_stop": self.broker_stop,
        }

    def runtime(self) -> dict[str, Any]:
        clock = self.clocks.get(GNB_REF_LINK)
        speed = clock.speed() if clock else None
        return {"real_time_ratio": speed, "slots_per_wall_second": speed * 1000 if speed else None,
                "broker": {k: self.broker_stop.get(k) for k in ("rx_starvations", "tx_queue_overflows",
                                                               "tx_sequence_gaps", "zmq_errors")}}

    def resource_grid(self) -> dict[str, Any]:
        return {"nof_prb": self.sched.nof_prb,
                "slots": [{"slot": s, "wall_ms": w // 1_000_000, "decision_us": d, "dl": dl, "ul": ul}
                          for s, w, d, dl, ul in self.sched.recent]}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-root", type=pathlib.Path, required=True)
    parser.add_argument("--channel-bundle", type=pathlib.Path, required=True)
    parser.add_argument("--traffic-bundle", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--warmup-seconds", type=int, default=0, help="leading radio seconds excluded from KPIs")
    args = parser.parse_args(argv)
    run = SingleCellRun(args.run_root, args.channel_bundle, args.traffic_bundle, args.warmup_seconds)
    run.poll()
    report = run.report(live=args.live)
    text = json.dumps(report, indent=1, sort_keys=True, default=analyze._json_default)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    m = report.get("metrics") or {}
    print(f"policy={report['policy']} segments={report['segments_valid']}/{report['segments_complete']} "
          f"dl_mbps={(m.get('aggregate_throughput_mbps') or {}).get('dl')} "
          f"jain={(m.get('jain_fairness') or {}).get('dl_throughput')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
