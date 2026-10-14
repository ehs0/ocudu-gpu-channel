#!/usr/bin/env python3
"""Measure what per-tap Doppler would and would not capture, before using it.

Replays the node positions and velocities recorded in a bridge status JSONL
through the same Sionna scene, solver and tap binning the bridge uses
(``SionnaScenario.trace`` and ``channel_adapter.bin_rays``/``rays_to_taps``),
and reports per link:

* bin merging -- paths per kept tap and the Doppler spread inside one 1/16
  sample delay bin, plus how well one representative Doppler per tap follows
  the bin's true coefficient over one update interval (power-weighted NMSE);
* boundary mismatch -- the channel at the next update predicted by rotating
  each tap of this update by its Doppler, against the next trace, on the
  occupied band, with and without the predicted delay drift;
* path physics -- for paths found again in the next trace (same interaction
  types on the same scene primitives), the phase predicted from
  ``phase + 2*pi*f_D*dt`` against the traced phase, and UL/DL Doppler ratio;
* delay drift -- per-tap delay change between updates, and how often the kept
  tap delays change at all (each change resets history in the emulator).

Nothing is sent to a broker. Output: one summary JSON (``--out``) and
optionally one JSONL row per update (``--rows``).

    ~/venvs/sionna/bin/python scripts/sionna_rt/measure_tap_doppler.py \
        --scenario-config examples/configs/sionna/scenarios/v2x_intersection/v2x-intersection-blocked.json \
        --replay <run>/sionna-status.jsonl --first 100 --count 3000 --out doppler.json
"""

from __future__ import annotations

import argparse
import cmath
import json
import math
import pathlib
import sys
import time
from typing import Any, Iterator, Sequence

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from sionna_rt import run_bridge  # noqa: E402
from sionna_rt.channel_adapter import Ray, bin_rays, rays_to_taps  # noqa: E402

DELAY_QUANTUM = 1.0 / 16.0
# Representative-Doppler candidates for one tap (design option C).
REPRESENTATIVES = ("zero", "power_mean", "strongest", "instantaneous")


def replay_records(path: pathlib.Path, first: int, count: int) -> Iterator[dict[str, Any]]:
    """Yield the positions/velocities/time of bridge updates first..first+count-1."""

    last = first + count
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if '"sionna_rt_update"' not in line[:40]:
                continue
            record = json.loads(line)
            iteration = int(record["iteration"])
            if iteration < first:
                continue
            if iteration >= last:
                return
            feed = record.get("position_status") or {}
            sample_ms = feed.get("last_sample_t_unix_ms")
            yield {
                "iteration": iteration,
                "t": float(record["elapsed_seconds"]),
                # When the positions were sampled by their publisher, if a live
                # feed supplied them; the bridge's own clock otherwise.
                "t_feed": None if sample_ms is None else sample_ms / 1000.0,
                "positions": record["positions"],
                "velocities": record["velocities_mps"],
            }


def percentiles(values: Sequence[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)

    def at(q: float) -> float:
        return ordered[min(n - 1, int(q * (n - 1) + 0.5))]

    return {
        "n": n,
        "p50": at(0.50),
        "p90": at(0.90),
        "p99": at(0.99),
        "max": ordered[-1],
        "mean": sum(ordered) / n,
    }


def wrap(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def tap_frequency(members: Sequence[Ray], which: str) -> float:
    """One representative Doppler for a bin of paths (design option C)."""

    if which == "zero":
        return 0.0
    powers = [abs(complex(ray.coefficient)) ** 2 for ray in members]
    if which == "power_mean":
        total = sum(powers)
        return sum(p * ray.doppler_hz for p, ray in zip(powers, members)) / total
    if which == "strongest":
        return members[max(range(len(members)), key=powers.__getitem__)].doppler_hz
    if which == "instantaneous":
        # d/dt arg(c(t)) / 2pi at t = 0 for c(t) = sum a_i exp(j 2 pi f_i t):
        # c'(0)/c(0) = j 2 pi (sum a_i f_i) / c, so the rate is Re((sum a_i f_i) / c).
        c = sum(complex(ray.coefficient) for ray in members)
        weighted = sum(complex(ray.coefficient) * ray.doppler_hz for ray in members)
        return (weighted / c).real if abs(c) > 0.0 else 0.0
    raise ValueError(which)


def bin_nmse(members: Sequence[Ray], f_rep: float, horizon_s: float, points: int = 51) -> tuple[float, float]:
    """Error energy and signal energy of c(0)exp(j2pi f_rep t) against the true
    bin coefficient sum_i a_i exp(j2pi f_i t), over t in [0, horizon]."""

    c0 = sum(complex(ray.coefficient) for ray in members)
    err = sig = 0.0
    for n in range(points):
        t = horizon_s * n / (points - 1)
        true = sum(
            complex(ray.coefficient) * cmath.exp(2j * math.pi * ray.doppler_hz * t)
            for ray in members
        )
        approx = c0 * cmath.exp(2j * math.pi * f_rep * t)
        err += abs(true - approx) ** 2
        sig += abs(true) ** 2
    return err, sig


def frequency_response(
    taps: Sequence[tuple[float, complex]], freqs_hz: Sequence[float], sample_rate_hz: float
) -> list[complex]:
    return [
        sum(c * cmath.exp(-2j * math.pi * f * d / sample_rate_hz) for d, c in taps)
        for f in freqs_hz
    ]


def nmse(a: Sequence[complex], b: Sequence[complex]) -> float:
    err = sum(abs(x - y) ** 2 for x, y in zip(a, b))
    ref = sum(abs(y) ** 2 for y in b)
    return err / ref if ref > 0.0 else float("nan")


def db(value: float) -> float:
    return 10.0 * math.log10(value) if value > 0.0 else float("-inf")


class LinkRays:
    """One link's usable rays plus a path identity that survives a re-trace."""

    def __init__(self, rays: list[Ray], signatures: dict[int, tuple[int, ...]]) -> None:
        self.rays = rays
        self.signatures = signatures  # id(ray) -> signature


def trace_links(scenario: Any, args: argparse.Namespace) -> dict[str, tuple[LinkRays, float]]:
    """Trace every configured link once, grouped as trace_all_profiles groups."""

    np = scenario.np
    out: dict[str, tuple[LinkRays, float]] = {}
    groups: dict[tuple[float, Any, Any], list[Any]] = {}
    for link in scenario.links:
        frequency_hz = (
            args.downlink_frequency_hz if link.direction == "downlink" else args.uplink_frequency_hz
        )
        key = (
            frequency_hz,
            scenario.definition.nodes[link.source].tx_array,
            scenario.definition.nodes[link.destination].rx_array,
        )
        groups.setdefault(key, []).append(link)
    for (frequency_hz, tx_array, rx_array), links in groups.items():
        scenario.configure_arrays(tx_array, rx_array)
        paths = scenario.trace(frequency_hz)
        coefficients, delays = paths.cir(
            sampling_frequency=args.update_hz,
            num_time_steps=1,
            normalize_delays=False,
            out_type="numpy",
        )
        coefficients = run_bridge.numpy_value(coefficients, np)
        delays = run_bridge.numpy_value(delays, np)
        dopplers = run_bridge.numpy_value(paths.doppler, np)
        valid = run_bridge.numpy_value(paths.valid, np)
        interactions = run_bridge.numpy_value(paths.interactions, np)
        primitives = run_bridge.numpy_value(paths.primitives, np)
        for link in links:
            tx_index = scenario.tx_indices[link.source]
            rx_index = scenario.rx_indices[link.destination]
            coeff = run_bridge.antenna_slice(coefficients, rx_index, tx_index, 0, 0, has_time=True)
            delay = run_bridge.antenna_slice(delays, rx_index, tx_index, 0, 0, has_time=False)
            doppler = run_bridge.antenna_slice(dopplers, rx_index, tx_index, 0, 0, has_time=False)
            ok = run_bridge.antenna_slice(valid, rx_index, tx_index, 0, 0, has_time=False)
            kinds = run_bridge.path_slice(interactions, rx_index, tx_index, trailing=0)
            prims = run_bridge.path_slice(primitives, rx_index, tx_index, trailing=0)
            rays: list[Ray] = []
            signatures: dict[int, tuple[int, ...]] = {}
            for p in range(len(coeff)):
                if not bool(ok[p]):
                    continue
                ray = Ray(float(delay[p]), complex(coeff[p]), float(doppler[p]))
                rays.append(ray)
                signature: list[int] = []
                for depth in range(kinds.shape[0]):
                    kind = int(kinds[depth, p])
                    if kind == 0:
                        break
                    signature.extend((kind, int(prims[depth, p])))
                signatures[id(ray)] = tuple(signature)
            link_id = run_bridge.control_link_id(link.source, link.destination, link.model)
            out[link_id] = (LinkRays(rays, signatures), frequency_hz)
    return out


def split_by_doppler(bins: dict[int, list[Ray]], width_hz: float) -> dict[tuple[int, int], list[Ray]]:
    groups: dict[tuple[int, int], list[Ray]] = {}
    for b, members in bins.items():
        for ray in members:
            groups.setdefault((b, int(math.floor(ray.doppler_hz / width_hz + 0.5))), []).append(ray)
    return groups


def analyze_update(
    link_id: str,
    current: LinkRays,
    following: LinkRays | None,
    dt: float,
    dt_bridge: float,
    frozen: bool,
    carrier_hz: float,
    args: argparse.Namespace,
    freqs_hz: Sequence[float],
    acc: dict[str, Any],
) -> dict[str, Any]:
    sr = args.sample_rate_hz
    bins = bin_rays(current.rays, sample_rate_hz=sr)
    taps = rays_to_taps(current.rays, sample_rate_hz=sr, gain_offset_db=args.gain_offset_db)
    kept = [int(round(tap.delay_samples / DELAY_QUANTUM)) for tap in taps if tap.gain_db > -100.0]
    kept = [b for b in kept if b in bins]
    row: dict[str, Any] = {"link": link_id, "paths": len(current.rays), "taps": len(kept)}

    # --- 5: bin merging -------------------------------------------------------
    power_total = 0.0
    nmse_err = {name: 0.0 for name in REPRESENTATIVES}
    nmse_sig = 0.0
    tap_rep: dict[int, dict[str, float]] = {}
    for b in kept:
        members = bins[b]
        p = abs(sum(complex(r.coefficient) for r in members)) ** 2
        power_total += p
        dopplers = [r.doppler_hz for r in members]
        acc["paths_per_tap"].append(len(members))
        acc["bin_doppler_range_hz"].append(max(dopplers) - min(dopplers))
        if len(members) > 1:
            acc["multi_path_bin_doppler_range_hz"].append(max(dopplers) - min(dopplers))
            acc["multi_path_bin_power"] += p
        acc["kept_bin_power"] += p
        tap_rep[b] = {name: tap_frequency(members, name) for name in REPRESENTATIVES}
        for name in REPRESENTATIVES:
            err, sig = bin_nmse(members, tap_rep[b][name], args.horizon_s)
            nmse_err[name] += err
            if name == REPRESENTATIVES[0]:
                nmse_sig += sig
    for name in REPRESENTATIVES:
        if nmse_sig > 0.0:
            acc["bin_nmse_db"][name].append(db(nmse_err[name] / nmse_sig))
    row["bin_nmse_db"] = {
        name: (db(nmse_err[name] / nmse_sig) if nmse_sig > 0.0 else None) for name in REPRESENTATIVES
    }

    # Option A: split each bin further by Doppler. Cost it in taps, in the error
    # it leaves over the horizon, and in how often its layout changes (each
    # change of tap count or delays resets history in the emulator).
    for width in args.doppler_bin_hz:
        groups = split_by_doppler(bins, width)
        powers = sorted((abs(sum(complex(r.coefficient) for r in m)) ** 2 for m in groups.values()), reverse=True)
        total = sum(powers)
        kept_power = sum(powers[:32])
        stats = acc["option_a"][str(width)]
        stats["taps_before_cap"].append(len(powers))
        stats["power_lost_db"].append(
            db(1.0 - kept_power / total) if total > 0.0 and kept_power < total else -300.0
        )
        err = sig = 0.0
        for key, members in groups.items():
            if key[0] not in kept:
                continue
            e, s = bin_nmse(members, tap_frequency(members, "power_mean"), args.horizon_s)
            err += e
            sig += s
        if sig > 0.0:
            stats["bin_nmse_db"].append(db(err / sig))
        if following is not None and not frozen:
            nxt = split_by_doppler(
                bin_rays(following.rays, sample_rate_hz=sr), width
            )
            stats["layout_changes"].append(
                float(sorted(k[0] for k in groups) != sorted(k[0] for k in nxt))
            )
    acc["option_a"]["delay_only"]["taps_before_cap"].append(len(bins))

    if following is None or dt <= 0.0 or frozen:
        return row

    # --- 3: delay drift and layout changes -----------------------------------
    next_taps = rays_to_taps(following.rays, sample_rate_hz=sr, gain_offset_db=args.gain_offset_db)
    layout_changed = [t.delay_samples for t in taps] != [t.delay_samples for t in next_taps]
    acc["layout_changes"] += int(layout_changed)
    acc["layout_pairs"] += 1
    row["layout_changed"] = layout_changed

    # --- path physics: phase + 2 pi f_D dt against the next trace -------------
    next_by_signature: dict[tuple[int, ...], Ray] = {}
    duplicate: set[tuple[int, ...]] = set()
    for ray in following.rays:
        signature = following.signatures[id(ray)]
        if signature in next_by_signature:
            duplicate.add(signature)
        next_by_signature[signature] = ray
    matched_power = total_power = 0.0
    for ray in current.rays:
        signature = current.signatures[id(ray)]
        p = abs(complex(ray.coefficient)) ** 2
        total_power += p
        if signature in duplicate or signature not in next_by_signature:
            continue
        nxt = next_by_signature[signature]
        matched_power += p
        predicted = cmath.phase(complex(ray.coefficient)) + 2.0 * math.pi * ray.doppler_hz * dt
        static_error = abs(wrap(cmath.phase(complex(nxt.coefficient)) - cmath.phase(complex(ray.coefficient))))
        rotated_error = abs(wrap(cmath.phase(complex(nxt.coefficient)) - predicted))
        bridge_error = abs(wrap(
            cmath.phase(complex(nxt.coefficient))
            - cmath.phase(complex(ray.coefficient))
            - 2.0 * math.pi * ray.doppler_hz * dt_bridge
        ))
        weight = p
        acc["path_phase_error_rad"]["static"].append((static_error, weight))
        acc["path_phase_error_rad"]["rotated"].append((rotated_error, weight))
        acc["path_phase_error_rad"]["rotated_bridge_clock"].append((bridge_error, weight))
        drift_samples = (nxt.delay_seconds - ray.delay_seconds) * sr
        predicted_drift = -ray.doppler_hz / carrier_hz * dt * sr
        acc["path_delay_drift_samples"].append(abs(drift_samples))
        acc["path_delay_drift_residual_samples"].append(abs(drift_samples - predicted_drift))
        acc["path_doppler_change_hz"].append(abs(nxt.doppler_hz - ray.doppler_hz))
    if total_power > 0.0:
        acc["path_match_power_fraction"].append(matched_power / total_power)

    # --- 6: channel-level mismatch at the boundary ---------------------------
    by_bin = {b: sum(complex(r.coefficient) for r in bins[b]) for b in kept}
    nxt_resp = frequency_response(
        [(t.delay_samples, cmath.rect(10.0 ** ((t.gain_db - args.gain_offset_db) / 20.0), t.phase_rad))
         for t in next_taps if t.gain_db > -100.0],
        freqs_hz, sr,
    )
    row["boundary_nmse_db"] = {}
    for name in REPRESENTATIVES:
        for with_drift in (False, True):
            predicted = []
            for b in kept:
                f = tap_rep[b][name]
                delay = b * DELAY_QUANTUM
                if with_drift:
                    delay += -f / carrier_hz * dt * sr
                predicted.append((delay, by_bin[b] * cmath.exp(2j * math.pi * f * dt)))
            value = db(nmse(frequency_response(predicted, freqs_hz, sr), nxt_resp))
            label = name + ("+drift" if with_drift else "")
            acc["boundary_nmse_db"].setdefault(label, []).append(value)
            row["boundary_nmse_db"][label] = value
    # Tap-level phase jump with the leading candidate, taps matched by bin.
    next_by_bin = {
        int(round(t.delay_samples / DELAY_QUANTUM)): t for t in next_taps if t.gain_db > -100.0
    }
    for b in kept:
        if b not in next_by_bin:
            continue
        weight = abs(by_bin[b]) ** 2
        for name in ("zero", "power_mean"):
            predicted = cmath.phase(by_bin[b]) + 2.0 * math.pi * tap_rep[b][name] * dt
            jump = abs(wrap(next_by_bin[b].phase_rad - predicted))
            acc["tap_phase_jump_rad"][name].append((jump, weight))
    return row


def weighted_percentiles(pairs: Sequence[tuple[float, float]]) -> dict[str, float] | None:
    if not pairs:
        return None
    ordered = sorted(pairs)
    total = sum(w for _, w in ordered)
    result: dict[str, float] = {"n": len(ordered)}
    for label, q in (("p50", 0.5), ("p90", 0.9), ("p99", 0.99)):
        running = 0.0
        for value, weight in ordered:
            running += weight
            if running >= q * total:
                result[label] = value
                break
    result["max"] = ordered[-1][0]
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario-config", type=pathlib.Path, required=True)
    parser.add_argument("--replay", type=pathlib.Path, required=True,
                        help="bridge status JSONL whose positions/velocities are replayed")
    parser.add_argument("--first", type=int, default=0, help="first bridge iteration to replay")
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--horizon-s", type=float, default=0.1,
                        help="update interval the bin-merging error is evaluated over")
    parser.add_argument("--occupied-bandwidth-hz", type=float, default=19.08e6,
                        help="band the boundary NMSE is evaluated on (106 PRB x 180 kHz)")
    parser.add_argument("--doppler-bin-hz", type=float, nargs="*", default=[1.0, 5.0, 20.0],
                        help="option A Doppler bin widths to cost")
    parser.add_argument("--links", default="ue0", help="only links whose id contains this text")
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--rows", type=pathlib.Path)
    opts = parser.parse_args(argv)

    args = run_bridge.parse_args(["--scenario-config", str(opts.scenario_config), "--dry-run"])
    args.horizon_s = opts.horizon_s
    args.doppler_bin_hz = opts.doppler_bin_hz
    scenario = run_bridge.SionnaScenario(args)
    points = 128
    freqs_hz = [
        -opts.occupied_bandwidth_hz / 2.0 + opts.occupied_bandwidth_hz * (k + 0.5) / points
        for k in range(points)
    ]

    def new_acc() -> dict[str, Any]:
        return {
            "paths_per_tap": [],
            "bin_doppler_range_hz": [],
            "multi_path_bin_doppler_range_hz": [],
            "multi_path_bin_power": 0.0,
            "kept_bin_power": 0.0,
            "bin_nmse_db": {name: [] for name in REPRESENTATIVES},
            "option_a": {
                str(w): {"taps_before_cap": [], "power_lost_db": [], "bin_nmse_db": [], "layout_changes": []}
                for w in opts.doppler_bin_hz
            }
            | {"delay_only": {"taps_before_cap": []}},
            "layout_changes": 0,
            "layout_pairs": 0,
            "frozen_pairs": 0,
            "path_phase_error_rad": {"static": [], "rotated": [], "rotated_bridge_clock": []},
            "path_delay_drift_samples": [],
            "path_delay_drift_residual_samples": [],
            "path_doppler_change_hz": [],
            "path_match_power_fraction": [],
            "boundary_nmse_db": {},
            "tap_phase_jump_rad": {"zero": [], "power_mean": []},
            "max_abs_doppler_hz": 0.0,
            "moving_updates": 0,
        }

    accs: dict[str, dict[str, Any]] = {}
    doppler_ratio: list[float] = []
    rows = opts.rows.open("w", encoding="utf-8") if opts.rows else None
    previous: tuple[dict[str, Any], dict[str, tuple[LinkRays, float]]] | None = None
    started = time.monotonic()
    replayed = 0

    def finish(prev_record: dict[str, Any], prev_links: dict[str, tuple[LinkRays, float]],
               next_record: dict[str, Any] | None, next_links: dict[str, tuple[LinkRays, float]] | None) -> None:
        dt_bridge = (next_record["t"] - prev_record["t"]) if next_record else 0.0
        # The phases of a trace belong to the instant its positions were
        # sampled, so the feed's own timestamps give the interval to rotate by.
        dt = dt_bridge
        if next_record and next_record["t_feed"] is not None and prev_record["t_feed"] is not None:
            dt = next_record["t_feed"] - prev_record["t_feed"]
        speed = math.sqrt(sum(v * v for v in prev_record["velocities"].get("ue0", [0, 0, 0])))
        # A held feed sample: the node did not move, but its last velocity (and
        # so Sionna's Doppler) is still nonzero. Counted, kept out of the
        # boundary statistics, and reported.
        frozen = bool(
            next_record
            and speed > 0.5
            and prev_record["positions"]["ue0"] == next_record["positions"]["ue0"]
        )
        out_row: dict[str, Any] = {
            "iteration": prev_record["iteration"], "dt": dt, "dt_bridge": dt_bridge,
            "frozen": frozen, "ue0_speed": speed, "links": [],
        }
        for link_id, (link_rays, carrier) in prev_links.items():
            if opts.links not in link_id:
                continue
            acc = accs.setdefault(link_id, new_acc())
            if speed > 0.5:
                acc["moving_updates"] += 1
            acc["frozen_pairs"] += int(frozen)
            for ray in link_rays.rays:
                acc["max_abs_doppler_hz"] = max(acc["max_abs_doppler_hz"], abs(ray.doppler_hz))
            following = next_links[link_id][0] if next_links else None
            out_row["links"].append(
                analyze_update(link_id, link_rays, following, dt, dt_bridge, frozen, carrier,
                               args, freqs_hz, acc)
            )
        # UL/DL Doppler ratio on the strongest path of ue0's two links.
        dl = [v for k, v in prev_links.items() if k.startswith("gnb0>ue0")]
        ul = [v for k, v in prev_links.items() if k.startswith("ue0>gnb0")]
        if dl and ul and dl[0][0].rays and ul[0][0].rays:
            a = max(dl[0][0].rays, key=lambda r: abs(complex(r.coefficient)))
            b = max(ul[0][0].rays, key=lambda r: abs(complex(r.coefficient)))
            if abs(a.doppler_hz) > 5.0:
                doppler_ratio.append(b.doppler_hz / a.doppler_hz)
        if rows is not None:
            rows.write(json.dumps(out_row, separators=(",", ":")) + "\n")

    for record in replay_records(opts.replay, opts.first, opts.count):
        for node_id in scenario.motion:
            position = tuple(float(v) for v in record["positions"][node_id])
            velocity = tuple(float(v) for v in record["velocities"].get(node_id, (0.0, 0.0, 0.0)))
            scenario.scene.get(f"{node_id}_tx").position = position
            scenario.scene.get(f"{node_id}_rx").position = position
            scenario.scene.get(f"{node_id}_tx").velocity = velocity
            scenario.scene.get(f"{node_id}_rx").velocity = velocity
        links = trace_links(scenario, args)
        if previous is not None:
            finish(previous[0], previous[1], record, links)
        previous = (record, links)
        replayed += 1
    if previous is not None:
        finish(previous[0], previous[1], None, None)
    if rows is not None:
        rows.close()

    summary: dict[str, Any] = {
        "scenario_config": str(opts.scenario_config),
        "replay": str(opts.replay),
        "first_iteration": opts.first,
        "updates": replayed,
        "wall_seconds": time.monotonic() - started,
        "sample_rate_hz": args.sample_rate_hz,
        "carriers_hz": {"downlink": args.downlink_frequency_hz, "uplink": args.uplink_frequency_hz},
        "horizon_s": opts.horizon_s,
        "occupied_bandwidth_hz": opts.occupied_bandwidth_hz,
        "ul_over_dl_strongest_path_doppler": percentiles(doppler_ratio),
        "expected_ul_over_dl": args.uplink_frequency_hz / args.downlink_frequency_hz,
        "links": {},
    }
    for link_id, acc in accs.items():
        summary["links"][link_id] = {
            "moving_updates": acc["moving_updates"],
            "frozen_feed_pairs": acc["frozen_pairs"],
            "max_abs_doppler_hz": acc["max_abs_doppler_hz"],
            "paths_per_tap": percentiles(acc["paths_per_tap"]),
            "bin_doppler_range_hz": percentiles(acc["bin_doppler_range_hz"]),
            "multi_path_bin_doppler_range_hz": percentiles(acc["multi_path_bin_doppler_range_hz"]),
            "multi_path_bin_power_fraction": (
                acc["multi_path_bin_power"] / acc["kept_bin_power"] if acc["kept_bin_power"] else None
            ),
            "bin_nmse_db_over_horizon": {k: percentiles(v) for k, v in acc["bin_nmse_db"].items()},
            "option_a": {
                k: {kk: percentiles(vv) for kk, vv in v.items()} for k, v in acc["option_a"].items()
            },
            "layout_change_fraction": (
                acc["layout_changes"] / acc["layout_pairs"] if acc["layout_pairs"] else None
            ),
            "path_match_power_fraction": percentiles(acc["path_match_power_fraction"]),
            "path_phase_error_rad_power_weighted": {
                k: weighted_percentiles(v) for k, v in acc["path_phase_error_rad"].items()
            },
            "path_delay_drift_samples": percentiles(acc["path_delay_drift_samples"]),
            "path_delay_drift_residual_samples": percentiles(acc["path_delay_drift_residual_samples"]),
            "path_doppler_change_hz": percentiles(acc["path_doppler_change_hz"]),
            "boundary_nmse_db": {k: percentiles(v) for k, v in acc["boundary_nmse_db"].items()},
            "tap_phase_jump_rad_power_weighted": {
                k: weighted_percentiles(v) for k, v in acc["tap_phase_jump_rad"].items()
            },
        }
    opts.out.parent.mkdir(parents=True, exist_ok=True)
    opts.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"updates": replayed, "out": str(opts.out), "wall_seconds": summary["wall_seconds"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
