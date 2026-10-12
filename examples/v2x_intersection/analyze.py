#!/usr/bin/env python3
"""Join one run's logs per episode: vehicles, V2X delivery, channel, UE link, broker.

    ~/venvs/v2x/bin/v2x-python examples/v2x_intersection/analyze.py <run dir>

`<run dir>` (`--out` elsewhere) is a gate log directory
(`$OCUDU_NATIVE_ROOT/results/logs/ocudu-multi-ue/<ts>/`, V2X files under
`v2x/`) or a `run_local.py --out` directory. Writes `<run dir>/v2x-analysis/`
`summary.json` and `episodes.csv` and prints the episode table.

Per episode (the wall-clock window of its drive.py loop):

* vehicle outcome from `result.json` (collision, minimum centre gap, first
  WARNING/BRAKING, crossing order);
* V2X delivery: roadside datagrams sent in the window, how many the UE
  received, one-way delay (one host clock: roadside and UE client share it),
  and the longest gap between receptions;
* channel (gate or `run_local.py --bridge`): the bridge's downlink to `ue0`
  — strongest tap gain, ray count — and its timing: trace time, generate to
  broker ACK, and position-to-ACK age (feed publish time to broker ACK, i.e.
  how old A's position is when the channel built from it is applied);
* UE link (gate only): srsUE's DL SNR and BLER in the window.

Run level (gate only): the broker's per-second GPU stage times (`gpu_timings`:
H2D + kernel + D2H, averages over each second, not per-slot values), its
starvation counters and the radio's slot rate. Anything a run does not
contain is reported as null ("unavailable"), never filled in.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pathlib
import re
import statistics

SLOT_US = 1000.0         # 15 kHz SCS: one slot per millisecond (23 040 samples at 23.04 MS/s)


def pct(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(q / 100.0 * (len(ordered) - 1))))], 3)


def stats(values):
    return {"n": len(values), "p50": pct(values, 50), "p95": pct(values, 95), "p99": pct(values, 99),
            "max": None if not values else round(max(values), 3),
            "min": None if not values else round(min(values), 3)}


def read_csv(path: pathlib.Path, delimiter=","):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter=delimiter))


def load_episodes(v2x_dir: pathlib.Path):
    episodes = []
    for result_path in sorted((v2x_dir / "episodes").glob("*/result.json")):
        result = json.loads(result_path.read_text())
        start_ms = result["start_unix_ns"] / 1e6
        end_ms = start_ms + result["timing"]["wall_elapsed_s"] * 1000.0
        episodes.append({"name": result_path.parent.name, "dir": result_path.parent, "result": result,
                         "start_ms": start_ms, "end_ms": end_ms})
    return episodes


def load_bridge(run: pathlib.Path, node: str):
    path = next((p for p in (run / "sionna-status.jsonl", run / "bridge-status.jsonl") if p.exists()), None)
    if path is None:
        return []
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if '"sionna_rt_update"' not in line:
                continue
            record = json.loads(line)
            channel = next((c for c in record.get("channels", [])
                            if c.get("source") == "gnb0" and c.get("destination") == node), None)
            status = record.get("position_status") or {}
            timing = record.get("timing_ms") or {}
            ack = record.get("control_ack_unix_ms")
            sample_ms = status.get("last_sample_t_unix_ms")
            rows.append({
                "t_ms": record["update_started_unix_ms"],
                "source": (status.get("node_sources") or {}).get(node, record.get("position_source")),
                "position": record.get("positions", {}).get(node),
                "tap_db": None if channel is None else channel.get("strongest_tap_gain_db"),
                "rays": None if channel is None else channel.get("ray_count"),
                "trace_ms": timing.get("channel_generation"),
                "to_ack_ms": timing.get("generate_to_control_ack"),
                "position_to_ack_ms": (ack - sample_ms) if ack is not None and sample_ms is not None else None,
                "stale": status.get("stale"),
            })
    return rows


def load_srsue(run: pathlib.Path, ue: str):
    start = run / f"srsue-{ue}.start_unix_ms"
    rows = read_csv(run / f"srsue-metrics-{ue}.csv", delimiter=";")
    if not rows or not start.exists():
        return []
    base = int(start.read_text().strip())
    out = []
    for row in rows:
        try:
            rsrp, snr = float(row["rsrp"]), float(row["dl_snr"])
            if rsrp == 0.0 and snr == 0.0:
                continue        # srsUE writes all-zero rows when it has no measurement: not reported
            out.append({"t_ms": base + float(row["time"]), "dl_snr": snr,
                        "dl_bler": float(row["dl_bler"]), "attached": row.get("is_attached")})
        except (KeyError, ValueError, TypeError):      # truncated last row
            continue
    return out


def load_broker(run: pathlib.Path):
    path = run / "broker.log"
    if not path.exists():
        return None
    gpu, starv, slots, stalls, warmups, batches = [], {}, {}, 0, 0, 0
    field = re.compile(r"(\w+)=([^\s\]]+)")
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith("event=gpu_timings"):
                f = dict(field.findall(line))
                total = float(f["h2d_us"]) + float(f["kernel_us"]) + float(f["d2h_us"])
                if total > 0.0:
                    gpu.append({"t": int(f["t"]), "h2d_us": float(f["h2d_us"]), "kernel_us": float(f["kernel_us"]),
                                "d2h_us": float(f["d2h_us"]), "total_us": total})
            elif line.startswith("event=heartbeat"):
                f = dict(field.findall(line))
                if "starvations_total" in f:
                    starv[f["dev"]] = int(f["starvations_total"])
                m = re.search(r"producer\[state=\w+ slots=(\d+)", line)
                if m:
                    slots.setdefault(f["dev"], []).append((int(f["t"]), int(m.group(1))))
            elif line.startswith("event=node_stall "):
                stalls += 1
            elif line.startswith("event=control_warmup_begin"):
                warmups += 1
            elif line.startswith("event=control_batch_commit"):
                batches += 1
    rates = {}
    for dev, series in slots.items():
        active = [(t, s) for t, s in series if s > 0]
        if len(active) >= 2:
            (t0, s0), (t1, s1) = active[0], active[-1]
            rates[dev] = round((s1 - s0) / max(1, t1 - t0), 1)
    totals = [g["total_us"] for g in gpu]
    return {
        "gpu_total_us_per_second_avg": stats(totals),
        "gpu_kernel_us_per_second_avg": stats([g["kernel_us"] for g in gpu]),
        "gpu_seconds_over_slot": sum(1 for v in totals if v > SLOT_US),
        "slot_us": SLOT_US,
        "per_slot_deadline_misses": None,     # the broker logs no per-slot completion times
        "starvations_total": starv,
        "node_stalls": stalls,
        "slots_per_second": rates,
        "channel_batches_committed": batches,
        "history_warmups": warmups,
        "series": gpu,
    }


def window(rows, start_ms, end_ms):
    return [r for r in rows if start_ms <= r["t_ms"] <= end_ms]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", type=pathlib.Path)
    ap.add_argument("--node", default="ue0")
    ap.add_argument("--out", type=pathlib.Path, help="output directory (default <run>/v2x-analysis)")
    args = ap.parse_args(argv)
    run = args.run.resolve()
    v2x_dir = run / "v2x" if (run / "v2x" / "episodes").exists() else run
    out_dir = args.out or run / "v2x-analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    episodes = load_episodes(v2x_dir)
    tx = [{"seq": int(r["sequence"]), "t_ms": int(r["send_wall_time_ns"]) / 1e6}
          for r in read_csv(v2x_dir / "roadside_tx.csv")]
    rx = {}
    for r in read_csv(v2x_dir / "ue_rx.csv"):
        if int(r["sequence"]) >= 0:
            rx.setdefault(int(r["sequence"]), r)
    bridge = load_bridge(run, args.node)
    srsue = load_srsue(run, args.node)
    broker = load_broker(run)

    table = []
    for ep in episodes:
        res = ep["result"]
        sent = [t for t in tx if ep["start_ms"] <= t["t_ms"] <= ep["end_ms"]]
        got = [rx[t["seq"]] for t in sent if t["seq"] in rx]
        one_way = [float(r["one_way_ms"]) for r in got if r["one_way_ms"]]
        rx_times = sorted(int(r["receive_wall_time_ns"]) / 1e6 for r in got)
        gaps = [b - a for a, b in zip(rx_times, rx_times[1:])]
        ch = [r for r in window(bridge, ep["start_ms"], ep["end_ms"]) if r["source"] == "external"]
        taps = [r["tap_db"] for r in ch if r["tap_db"] is not None]
        ue = window(srsue, ep["start_ms"], ep["end_ms"])
        row = {
            "episode": ep["name"], "mode": res["label"], "ended": res["ended"], "collision": res["collision"],
            "min_centre_gap_m": res["min_centre_gap_m"], "first_risk_s": res["first_risk_sim_time_s"],
            "first_brake_s": res["first_brake_sim_time_s"], "a_crossed_s": res["a_crossed_sim_time_s"],
            "order": res["order"], "rtf": res["timing"]["rtf"], "late_steps": res["timing"]["late_steps"],
            "v2x_sent": len(sent), "v2x_received": len(got),
            "v2x_delivery": round(len(got) / len(sent), 4) if sent else None,
            "one_way_ms_p50": pct(one_way, 50), "one_way_ms_p95": pct(one_way, 95),
            "one_way_ms_max": None if not one_way else round(max(one_way), 3),
            "longest_rx_gap_ms": None if not gaps else round(max(gaps), 1),
            "channel_updates": len(ch),
            "tap_db_max": None if not taps else round(max(taps), 2),
            "tap_db_min": None if not taps else round(min(taps), 2),
            "trace_ms_p50": pct([r["trace_ms"] for r in ch if r["trace_ms"] is not None], 50),
            "to_ack_ms_p95": pct([r["to_ack_ms"] for r in ch if r["to_ack_ms"] is not None], 95),
            "position_to_ack_ms_p50": pct([r["position_to_ack_ms"] for r in ch if r["position_to_ack_ms"] is not None], 50),
            "position_to_ack_ms_p95": pct([r["position_to_ack_ms"] for r in ch if r["position_to_ack_ms"] is not None], 95),
            "ue_dl_snr_min": None if not ue else min(r["dl_snr"] for r in ue),
            "ue_dl_snr_median": None if not ue else statistics.median(r["dl_snr"] for r in ue),
            "ue_dl_bler_max": None if not ue else max(r["dl_bler"] for r in ue),
        }
        table.append(row)

    summary = {
        "run": str(run),
        "episodes": table,
        "by_mode": {},
        "broker": None if broker is None else {k: v for k, v in broker.items() if k != "series"},
        "sources": {"bridge_updates": len(bridge), "srsue_rows": len(srsue), "roadside_tx": len(tx),
                    "ue_rx": len(rx)},
    }
    for mode in sorted({r["mode"] for r in table}):
        rows = [r for r in table if r["mode"] == mode]
        summary["by_mode"][mode] = {
            "episodes": len(rows),
            "collisions": sum(1 for r in rows if r["collision"]),
            "min_centre_gap_m": stats([r["min_centre_gap_m"] for r in rows]),
            "v2x_delivery": stats([r["v2x_delivery"] for r in rows if r["v2x_delivery"] is not None]),
        }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if broker is not None:
        (out_dir / "broker-gpu-per-second.json").write_text(json.dumps(broker["series"]) + "\n", encoding="utf-8")
    if table:
        with open(out_dir / "episodes.csv", "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(table[0]))
            writer.writeheader()
            writer.writerows(table)
    cols = ["episode", "collision", "min_centre_gap_m", "first_risk_s", "first_brake_s", "v2x_delivery",
            "one_way_ms_p50", "one_way_ms_max", "longest_rx_gap_ms", "tap_db_min", "position_to_ack_ms_p50",
            "ue_dl_snr_min", "ue_dl_bler_max"]
    print("  ".join(cols))
    for row in table:
        print("  ".join("-" if row[c] is None else str(row[c]) for c in cols))
    if broker is not None:
        print(json.dumps(summary["broker"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
