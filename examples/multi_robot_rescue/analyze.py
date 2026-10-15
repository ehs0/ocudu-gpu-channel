#!/usr/bin/env python3
"""Per-episode metrics of a rescue run (stdlib only).

Input: one episode directory as run_local.py or launch/root_exec.sh writes it
(`run.json`, `sim/`, `robots/`, `links/<robot>/`, `server/`). Output:
`summary.json` and a one-row `summary.csv` in that directory.

Metrics (definitions in MULTI_ROBOT_RESCUE_MILESTONES.md):

- per survivor: first visible to a robot (referee), confirmed locally (robot
  log), reported centrally (first receive at the server), and the latencies
  between them;
- per robot: time in each link state, OFFLINE intervals and the time from
  each OFFLINE to the next ONLINE, peak backlog, delivered events and their
  capture-to-ACK latency per type, time in each autonomy mode;
- mission: success, end reason, coverage over time (time to 50 % / 80 %),
  central pose age (age of information) per robot.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pathlib
import statistics


def read_csv(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def spans(transitions: list[tuple[int, str]], start_ms: int, end_ms: int) -> list[tuple[str, int, int]]:
    """(state, from, to) spans from (t_ms, new_state) transitions, clipped to [start, end]."""

    out, state, since = [], "BOOT", start_ms
    for t, new in sorted(transitions):
        t = max(t, start_ms)
        if t > end_ms:
            break
        if t > since:
            out.append((state, since, t))
        state, since = new, t
    if end_ms > since:
        out.append((state, since, end_ms))
    return out


def analyze(episode: pathlib.Path) -> dict:
    run = json.loads((episode / "run.json").read_text(encoding="utf-8")) if (episode / "run.json").exists() else {}
    result = json.loads((episode / "sim" / "result.json").read_text(encoding="utf-8"))
    start_ms = result["start_ms"]
    end_ms = start_ms + int(result["wall_time_s"] * 1000)
    survivors = result["survivors"]

    # Local confirmations (robot logs), matched to the hidden survivors by position.
    confirmations = []
    robots_dir = episode / "robots"
    robot_rows = {}
    for path in sorted(robots_dir.glob("robot_*.jsonl")):
        rows = read_jsonl(path)
        rid = path.stem
        robot_rows[rid] = rows
        for row in rows:
            if row["event"] == "survivor_confirmed":
                confirmations.append(row)
    per_survivor = []
    for s in survivors:
        sx, sy = s["position_m"]
        local = [c for c in confirmations if math.hypot(c["position_m"][0] - sx, c["position_m"][1] - sy) <= 2.0]
        first_local = min(local, key=lambda c: c["t_ms"]) if local else None
        report = s.get("central_report")
        seen = s.get("first_seen")
        item = {
            "index": s["index"], "room": s["room"],
            "first_seen_s": None if not seen else round((seen["t_ms"] - start_ms) / 1000, 2),
            "first_seen_by": None if not seen else seen["robot_id"],
            "local_confirm_s": None if not first_local else round((first_local["t_ms"] - start_ms) / 1000, 2),
            "local_confirm_by": None if not first_local else first_local["robot_id"],
            "local_confirm_link_state": None if not first_local else first_local.get("link_state"),
            "central_received_s": None if not report or not report.get("received_at_ms")
            else round((report["received_at_ms"] - start_ms) / 1000, 2),
            "central_capture_s": None if not report or not report.get("captured_at_ms")
            else round((report["captured_at_ms"] - start_ms) / 1000, 2),
            "reported_by": None if not report else report.get("reported_by"),
            "position_error_m": None if not report else report.get("error_m"),
        }
        if item["local_confirm_s"] is not None and item["central_received_s"] is not None:
            item["confirm_to_report_s"] = round(item["central_received_s"] - item["local_confirm_s"], 2)
        per_survivor.append(item)

    per_robot = {}
    for link_dir in sorted((episode / "links").glob("robot_*")):
        rid = link_dir.name
        transitions = [(int(r["t_ms"]), r["to"]) for r in read_csv(link_dir / "link_state.csv")]
        state_spans = spans(transitions, start_ms, end_ms)
        time_in = {}
        for state, a, b in state_spans:
            time_in[state] = round(time_in.get(state, 0.0) + (b - a) / 1000, 2)
        offline = [(a, b) for state, a, b in state_spans if state == "OFFLINE"]
        recoveries = []
        for a, b in offline:
            nxt = [t for t, new in sorted(transitions) if t >= b and new == "ONLINE"]
            recoveries.append({"offline_from_s": round((a - start_ms) / 1000, 2),
                               "offline_to_s": round((b - start_ms) / 1000, 2),
                               "online_again_s": None if not nxt else round((nxt[0] - start_ms) / 1000, 2)})
        delivered = read_csv(link_dir / "delivered.csv")
        by_type: dict[str, list[float]] = {}
        for row in delivered:
            by_type.setdefault(row["type"], []).append((int(row["acked_ms"]) - int(row["captured_at_ms"])) / 1000)
        status = read_csv(link_dir / "link_status.csv")
        backlog = [int(r["backlog"]) for r in status if start_ms <= int(r["t_ms"]) <= end_ms]
        modes = {}
        rows = robot_rows.get(rid, [])
        mode_transitions = [(r["t_ms"], r["mode"]) for r in rows if r["event"] == "mode"]
        for mode, a, b in spans(mode_transitions, start_ms, end_ms):
            modes[mode] = round(modes.get(mode, 0.0) + (b - a) / 1000, 2)
        per_robot[rid] = {
            "time_in_link_state_s": time_in, "offline_intervals": recoveries,
            "backlog_peak": max(backlog) if backlog else None,
            "delivered_events": len(delivered),
            "delivery_latency_s": {t: {"n": len(v), "p50": round(pct(v, 0.5), 3), "p95": round(pct(v, 0.95), 3),
                                       "max": round(max(v), 3)} for t, v in sorted(by_type.items())},
            "time_in_mode_s": modes,
            "retransmitted_events": sum(max(0, int(r["send_count"]) - 1) for r in delivered),
            "contacts": result["robots"].get(rid, {}).get("contacts"),
            "distance_m": result["robots"].get(rid, {}).get("distance_m"),
        }

    referee = read_csv(episode / "sim" / "referee.csv")
    cov = [(float(r["sim_time_s"]), float(r["central_coverage"])) for r in referee]

    def time_to(level):
        return next((round(t, 1) for t, c in cov if c >= level), None)

    aoi = {}
    for row in read_csv(episode / "server" / "robot_status.csv"):
        if row["pose_age_s"] not in ("", "None"):
            aoi.setdefault(row["robot_id"], []).append(float(row["pose_age_s"]))
    server_state = {}
    if (episode / "server" / "final_state.json").exists():
        server_state = json.loads((episode / "server" / "final_state.json").read_text(encoding="utf-8"))
    summary = {
        "episode": episode.name, "network_mode": run.get("network_mode"), "radio": run.get("radio"),
        "world": result["world"], "seed": result["seed"], "preset": result["preset"],
        "success": result["success"], "end_reason": result["end_reason"], "sim_time_s": result["sim_time_s"],
        "rtf": result["timing"]["rtf"], "late_steps": result["timing"]["late_steps"],
        "coverage_final": result["coverage_final"], "time_to_coverage_50_s": time_to(0.5),
        "time_to_coverage_80_s": time_to(0.8), "survivors": per_survivor, "robots": per_robot,
        "central_pose_age_s": {rid: {"mean": round(statistics.fmean(v), 2), "max": round(max(v), 2)}
                               for rid, v in sorted(aoi.items())},
        "server_counters": server_state.get("counters"),
    }
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episodes", nargs="+", type=pathlib.Path)
    args = ap.parse_args(argv)
    for episode in args.episodes:
        summary = analyze(episode)
        (episode / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        flat = {k: summary[k] for k in ("episode", "network_mode", "world", "seed", "success", "end_reason",
                                        "sim_time_s", "rtf", "coverage_final", "time_to_coverage_50_s",
                                        "time_to_coverage_80_s")}
        for s in summary["survivors"]:
            flat[f"s{s['index']}_local_confirm_s"] = s["local_confirm_s"]
            flat[f"s{s['index']}_central_received_s"] = s["central_received_s"]
        for rid, r in summary["robots"].items():
            flat[f"{rid}_offline_s"] = r["time_in_link_state_s"].get("OFFLINE", 0.0)
            flat[f"{rid}_backlog_peak"] = r["backlog_peak"]
        with open(episode / "summary.csv", "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(flat))
            writer.writeheader()
            writer.writerow(flat)
        print(json.dumps(flat))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
