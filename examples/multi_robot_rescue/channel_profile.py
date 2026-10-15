#!/usr/bin/env python3
"""gNB <-> robot channel over the building floor, traced as the bridge does (Sionna venv).

Moves `ue0` over a grid of floor points (antenna height `layout.ROBOT_ANTENNA_HEIGHT_M`,
points inside walls or rubble skipped) and records what the bridge's own
`trace_all_profiles` reports for gnb0 <-> ue0: `strongest_tap_gain_db` (what the
broker applies, after `--gain-offset-db`), `total_path_power_db` and the ray
count. Prints a per-room summary; `--csv` keeps every point (the dashboard can
overlay it). This is the channel the emulator would apply, not a link outcome:
whether a room is an outage is measured in the live gate.

    $OCUDU_NATIVE_SIONNA_PYTHON examples/multi_robot_rescue/channel_profile.py \\
        --scenario-config examples/configs/sionna/scenarios/multi_robot_rescue/multi-robot-rescue-entrance-1robot.json \\
        --step-m 1.0 --csv rescue-coverage.csv
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import statistics
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
    ap.add_argument("--step-m", type=float, default=1.0)
    ap.add_argument("--rooms", default="", help="comma list of rooms/corridor to restrict to")
    ap.add_argument("--csv", type=pathlib.Path)
    ap.add_argument("--bridge-arg", action="append", default=[],
                    help="extra run_bridge.py argument, e.g. --bridge-arg=--gain-offset-db=70")
    args = ap.parse_args(argv)

    scenario = SionnaScenario(bridge_parse_args(["--scenario-config", str(args.scenario_config)]
                                                + list(args.bridge_arg)))
    scenario.update_positions(0.0)
    tx, rx = scenario.scene.get("ue0_tx"), scenario.scene.get("ue0_rx")
    tx.velocity = rx.velocity = (0.0, 0.0, 0.0)
    wanted = {r.strip() for r in args.rooms.split(",") if r.strip()}

    points = []
    x = layout.BUILDING_X_M[0] + args.step_m / 2
    while x < layout.BUILDING_X_M[1]:
        y = layout.BUILDING_Y_M[0] + args.step_m / 2
        while y < layout.BUILDING_Y_M[1]:
            room = layout.room_of(x, y)
            if not layout.blocked(x, y, 0.15) and (not wanted or room in wanted):
                points.append((round(x, 2), round(y, 2), room))
            y += args.step_m
        x += args.step_m

    rows = []
    started = time.monotonic()
    for x, y, room in points:
        tx.position = rx.position = (x, y, layout.ROBOT_ANTENNA_HEIGHT_M)
        _, statuses, timings = scenario.trace_all_profiles()

        def pick(source, destination):
            return next((s for s in statuses if s.get("source") == source and s.get("destination") == destination), {})

        down, up = pick("gnb0", "ue0"), pick("ue0", "gnb0")
        rows.append({"x_m": x, "y_m": y, "room": room, "dl_tap_db": down.get("strongest_tap_gain_db"),
                     "dl_power_db": down.get("total_path_power_db"), "dl_rays": down.get("ray_count"),
                     "ul_tap_db": up.get("strongest_tap_gain_db"), "trace_ms": round(sum(timings.values()), 1)})
    elapsed = time.monotonic() - started
    by_room: dict[str, list[float]] = {}
    for row in rows:
        value = row["dl_tap_db"]
        by_room.setdefault(row["room"], []).append(-200.0 if value is None else float(value))
    print(f"{'room':10s} {'n':>4s} {'median':>8s} {'min':>8s} {'max':>8s}   strongest DL tap (dB)")
    for room in sorted(by_room):
        v = by_room[room]
        print(f"{room:10s} {len(v):4d} {statistics.median(v):8.1f} {min(v):8.1f} {max(v):8.1f}")
    trace = [r["trace_ms"] for r in rows]
    print(f"{len(rows)} points in {elapsed:.1f} s; trace p50 {statistics.median(trace):.1f} ms, max {max(trace):.1f} ms")
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
