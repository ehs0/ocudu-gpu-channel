#!/usr/bin/env python3
"""Boundary mismatch of per-tap Doppler in a live run (docs/plans/sionna-tap-doppler.md).

Joins the broker's ``event=tap_rotation_anchor`` lines (link, seqno, anchor
sample, wall time, phase time) with the bridge's status JSONL (each update's
taps with their Doppler, and the seqno the broker acknowledged per link). For
consecutive profiles k, k+1 of one link it rotates profile k to profile k+1's
anchor sample exactly as the emulator does, and compares the resulting
frequency response on the occupied band with profile k+1 at its own start:

    NMSE = sum_f |H_k(f; n_{k+1}) - H_{k+1}(f; n_{k+1})|^2 / sum_f |H_{k+1}|^2

for four time bases, so each part of the mechanism is measured separately:
``static`` (no rotation, the behaviour before this feature), ``apply_slot``
(rotate from the anchor in sample time, no phase time), ``phase_time``
(age from the phase time, rate 1) and ``emulator`` (age and the median
wall/sample rate -- what the emulator runs).

    python3 scripts/sionna_rt/analyze_tap_doppler_run.py <log dir> --out <json>
"""

from __future__ import annotations

import argparse
import cmath
import json
import math
import pathlib
import re
import statistics
from typing import Any

RATE_WINDOW = 16  # TapRotationClock::kRateWindow
ANCHOR = re.compile(
    r"event=tap_rotation_anchor slot=(\d+) link_id=(\S+) seqno=(\d+) anchor_samples=(\d+) "
    r"wall_unix_ns=(\d+) phase_time_unix_ns=(\d+)"
)


def pct(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    s = sorted(values)
    at = lambda q: s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))]  # noqa: E731
    return {"n": len(s), "p10": at(0.1), "p50": at(0.5), "p90": at(0.9), "p99": at(0.99), "max": s[-1]}


def response(taps: list[dict[str, Any]], freqs: list[float], fs: float, t: float) -> list[complex]:
    out = []
    for f in freqs:
        h = 0j
        for tap in taps:
            amp = 10.0 ** (tap["gain_db"] / 20.0)
            phase = tap["phase_rad"] + 2.0 * math.pi * tap.get("doppler_hz", 0.0) * t
            h += amp * cmath.exp(1j * phase) * cmath.exp(-2j * math.pi * f * tap["delay_samples"] / fs)
        out.append(h)
    return out


def nmse_db(a: list[complex], b: list[complex]) -> float:
    err = sum(abs(x - y) ** 2 for x, y in zip(a, b))
    ref = sum(abs(y) ** 2 for y in b)
    return 10.0 * math.log10(err / ref) if err > 0 and ref > 0 else float("-inf")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log_dir", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--links", default="ue0", help="only links whose id contains this text")
    ap.add_argument("--occupied-bandwidth-hz", type=float, default=19.08e6)
    ap.add_argument("--sample-rate-hz", type=float, default=23.04e6)
    args = ap.parse_args()
    fs = args.sample_rate_hz
    freqs = [-args.occupied_bandwidth_hz / 2 + args.occupied_bandwidth_hz * (k + 0.5) / 64 for k in range(64)]

    anchors: dict[str, list[dict[str, int]]] = {}
    with (args.log_dir / "broker.log").open(errors="replace") as handle:
        for line in handle:
            m = ANCHOR.match(line)
            if m and args.links in m.group(2):
                anchors.setdefault(m.group(2), []).append({
                    "seqno": int(m.group(3)), "samples": int(m.group(4)),
                    "wall": int(m.group(5)), "phase_time": int(m.group(6))})

    # seqno -> (taps of lane 0, ue0 moved since the previous update)
    profiles: dict[str, dict[int, tuple[list[dict[str, Any]], bool]]] = {}
    previous_position = None
    with (args.log_dir / "sionna-status.jsonl").open() as handle:
        for line in handle:
            if '"sionna_rt_update"' not in line[:40]:
                continue
            record = json.loads(line)
            position = (record.get("positions") or {}).get("ue0")
            moved = previous_position is not None and position != previous_position
            previous_position = position
            reply = record.get("control_reply") or {}
            seqnos = {item["link_id"]: item["seqno"] for item in reply.get("links", [])}
            for channel in record.get("channels", []):
                link = channel["link_id"]
                if args.links in link and link in seqnos:
                    profiles.setdefault(link, {})[seqnos[link]] = (channel["lanes"][0]["taps"], moved)

    summary: dict[str, Any] = {"log_dir": str(args.log_dir), "links": {}}
    for link, items in anchors.items():
        bases = {"static": [], "apply_slot": [], "phase_time": [], "emulator": []}
        rates: list[float] = []
        ages: list[float] = []
        missing = 0
        for k in range(len(items) - 1):
            a, b = items[k], items[k + 1]
            pa = profiles.get(link, {}).get(a["seqno"])
            pb = profiles.get(link, {}).get(b["seqno"])
            if pa is None or pb is None:
                missing += 1
                continue
            if not pb[1]:
                continue  # ue0 did not move: a held feed sample, reported by the replay
            window = items[max(0, k - RATE_WINDOW + 1): k + 1]
            interval_rates = [
                (w2["wall"] - w1["wall"]) * 1e-9 / ((w2["samples"] - w1["samples"]) / fs)
                for w1, w2 in zip(window, window[1:]) if w2["samples"] > w1["samples"]
            ]
            rate = statistics.median_low(sorted(interval_rates)[:]) if len(interval_rates) % 2 else (
                sorted(interval_rates)[len(interval_rates) // 2] if interval_rates else 1.0)
            age_a = (a["wall"] - a["phase_time"]) * 1e-9
            age_b = (b["wall"] - b["phase_time"]) * 1e-9
            elapsed = (b["samples"] - a["samples"]) / fs
            rates.append(rate)
            ages.append(age_a)
            new = response(pb[0], freqs, fs, age_b)
            new_no_age = response(pb[0], freqs, fs, 0.0)
            bases["static"].append(nmse_db(response(pa[0], freqs, fs, 0.0), new_no_age))
            bases["apply_slot"].append(nmse_db(response(pa[0], freqs, fs, elapsed), new_no_age))
            bases["phase_time"].append(nmse_db(response(pa[0], freqs, fs, age_a + elapsed), new))
            bases["emulator"].append(nmse_db(response(pa[0], freqs, fs, age_a + rate * elapsed), new))
        summary["links"][link] = {
            "anchors": len(items),
            "unmatched_pairs": missing,
            "boundary_nmse_db": {k: pct(v) for k, v in bases.items()},
            "rate_wall_per_sample": pct(rates),
            "age_at_anchor_s": pct(ages),
        }
    args.out.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
