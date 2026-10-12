#!/usr/bin/env python3
"""Downlink/uplink channel of vehicle A along its approach, traced as the bridge does.

Moves `ue0` along A's lane (x = layout.A_LANE_X_M) from `--y-from` to
`--y-to` and records what the bridge's own `trace_all_profiles` reports for
gnb0 <-> ue0: `strongest_tap_gain_db` (what the broker applies, after
`--gain-offset-db`), `total_path_power_db` (physical), the ray count, and
whether the 2-D layout gives line of sight. Run with the Sionna venv:

    $OCUDU_NATIVE_SIONNA_PYTHON examples/v2x_intersection/channel_profile.py \
        --scenario-config examples/configs/sionna/scenarios/v2x_intersection/v2x-intersection-blocked.json \
        --csv blocked-profile.csv
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "scripts" / "sionna_rt"))
sys.path.insert(0, str(HERE))

import layout  # noqa: E402
from run_bridge import SionnaScenario, parse_args as bridge_parse_args  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario-config", required=True, type=pathlib.Path)
    ap.add_argument("--y-from", type=float, default=layout.A_START_M[1])
    ap.add_argument("--y-to", type=float, default=layout.CONFLICT_POINT_M[1])
    ap.add_argument("--step-m", type=float, default=2.0)
    ap.add_argument("--csv", type=pathlib.Path)
    ap.add_argument("--bridge-arg", action="append", default=[],
                    help="extra run_bridge.py argument, e.g. --bridge-arg=--gain-offset-db=70")
    args = ap.parse_args(argv)

    scenario = SionnaScenario(bridge_parse_args(["--scenario-config", str(args.scenario_config)]
                                                + list(args.bridge_arg)))
    scenario.update_positions(0.0)
    gnb = scenario.definition.nodes["gnb0"].motion.start
    tx, rx = scenario.scene.get("ue0_tx"), scenario.scene.get("ue0_rx")
    tx.velocity = rx.velocity = (0.0, 0.0, 0.0)

    rows = []
    started = time.monotonic()
    count = int(round((args.y_to - args.y_from) / args.step_m))
    for k in range(count + 1):
        y = args.y_from + k * args.step_m
        position = (layout.A_LANE_X_M, y, layout.ANTENNA_HEIGHT_M)
        tx.position = rx.position = position
        _, statuses, timings = scenario.trace_all_profiles()

        def pick(source, destination):
            return next((s for s in statuses
                         if s.get("source") == source and s.get("destination") == destination), {})

        down, up = pick("gnb0", "ue0"), pick("ue0", "gnb0")
        rows.append({
            "y_m": round(y, 2),
            "d_conflict_m": round(layout.CONFLICT_POINT_M[1] - y, 2),
            "los_2d": int(layout.line_of_sight(gnb[:2], position[:2])),
            "dl_tap_db": down.get("strongest_tap_gain_db"),
            "dl_power_db": down.get("total_path_power_db"),
            "dl_rays": down.get("ray_count"),
            "ul_tap_db": up.get("strongest_tap_gain_db"),
            "trace_ms": round(sum(timings.values()), 1),
        })
        r = rows[-1]
        print(f"y={r['y_m']:7.1f} d={r['d_conflict_m']:6.1f} los={r['los_2d']} "
              f"dl_tap={r['dl_tap_db']} dB power={r['dl_power_db']} rays={r['dl_rays']} "
              f"trace={r['trace_ms']} ms", flush=True)
    print(f"{len(rows)} points in {time.monotonic() - started:.1f} s")
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
