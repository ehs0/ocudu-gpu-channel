#!/usr/bin/env python3
"""Figures for one run (reads the same files as analyze.py).

    ~/venvs/v2x/bin/v2x-python examples/v2x_intersection/plot_run.py <run dir> [--out DIR]

Writes PNGs into `<run dir>/v2x-analysis/` (or `--out`):

* `trajectories.png` — plan view with A's position at the moment B reaches
  the conflict point, and both vehicles' distance to the conflict point over
  time (first episode of each mode);
* `episode-<name>.png` — one per episode: A's speed and target speed, A's
  acceleration command (braking negative), V2X one-way delay per received
  message, and the bridge's downlink tap gain to A's UE, all on the
  episode's simulation time;
* `gpu.png` — the broker's per-second GPU stage time (H2D + kernel + D2H,
  average per slot within each second) against the 1 ms slot;
* `response.png` — per episode, the longest V2X reception gap against the
  time of A's first WARNING/BRAKING.

Colours: the dataviz skill's reference categorical slots 1-3 in fixed order
per mode; text stays in neutral ink.
"""

from __future__ import annotations

import argparse
import csv
import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

import analyze  # noqa: E402
import layout  # noqa: E402

SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
INK, MUTED, GRID, SURFACE = "#1f1f1e", "#6b6a63", "#e4e3dd", "#fcfcfb"
BLOCK = "#d9d8d2"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": MUTED, "axes.labelcolor": INK, "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "axes.spines.top": False,
    "axes.spines.right": False, "lines.linewidth": 2.0, "font.size": 9, "legend.frameon": False,
})


def mode_colours(modes):
    return {mode: SLOTS[i % len(SLOTS)] for i, mode in enumerate(sorted(modes))}


def vehicle_rows(episode_dir: pathlib.Path):
    with open(episode_dir / "vehicles.csv", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    a = [r for r in rows if r["vehicle_id"] == "A"]
    b = [r for r in rows if r["vehicle_id"] == "B"]
    return a, b


def draw_layout(ax):
    for block in layout.BLOCKS:
        ax.add_patch(plt.Rectangle((block.x_min, block.y_min), *block.size, color=BLOCK, lw=0))
    ax.plot(*layout.CONFLICT_POINT_M, marker="x", color=INK, ms=8, lw=0)
    ax.annotate("conflict point", layout.CONFLICT_POINT_M, xytext=(6, 6), textcoords="offset points", color=MUTED)


def plot_trajectories(episodes, colours, out):
    """Left: plan view, A's position at the moment B reaches the conflict point.
    Right: each vehicle's distance to the conflict point over time."""

    fig, (plan, dist) = plt.subplots(1, 2, figsize=(9.6, 4.6), gridspec_kw={"width_ratios": [1, 1.25]})
    draw_layout(plan)
    cp = layout.CONFLICT_POINT_M
    seen = set()
    for ep in episodes:
        mode = ep["result"]["label"]
        if mode in seen:
            continue
        seen.add(mode)
        a, b = vehicle_rows(ep["dir"])
        t = [float(r["simulation_time"]) for r in a]
        a_d = [cp[1] - float(r["position_y"]) for r in a]
        b_d = [float(r["position_x"]) - cp[0] for r in b]
        dist.plot(t, a_d, color=colours[mode], label=f"A, {mode}")
        if len(seen) == 1:
            dist.plot(t, b_d, color=MUTED, ls="--", lw=1.5, label="B (scripted)")
        at_b = next((i for i, d in enumerate(b_d) if d <= 0.0), None)
        if at_b is not None:
            plan.plot(float(a[at_b]["position_x"]), float(a[at_b]["position_y"]), "o", ms=9,
                      color=colours[mode], mec=SURFACE, mew=2, label=f"A, {mode}")
    plan.plot([cp[0], cp[0]], [-100, 40], color=MUTED, lw=1.0)
    plan.plot([-40, 110], [cp[1], cp[1]], color=MUTED, lw=1.0, ls="--")
    plan.set_xlim(-40, 110)
    plan.set_ylim(-100, 40)
    plan.set_aspect("equal")
    plan.set_xlabel("x east (m)")
    plan.set_ylabel("y north (m)")
    plan.set_title("A when B reaches the conflict point", color=INK, loc="left")
    plan.legend(loc="upper right", fontsize=7)
    dist.axhline(0.0, color=MUTED, lw=0.8)
    dist.axhline(layout.STOP_LINE_GAP_M, color=MUTED, lw=0.8, ls=":")
    dist.annotate("stop line", (0, layout.STOP_LINE_GAP_M), xytext=(2, 3), textcoords="offset points", color=MUTED)
    dist.set_xlabel("simulation time (s)")
    dist.set_ylabel("distance to conflict point (m)")
    dist.set_title("Approach (first episode per mode)", color=INK, loc="left")
    dist.legend(loc="upper right", fontsize=7)
    fig.tight_layout()
    fig.savefig(out / "trajectories.png", dpi=160)
    plt.close(fig)


def plot_episode(ep, colour, bridge, out):
    a, _ = vehicle_rows(ep["dir"])
    t = [float(r["simulation_time"]) for r in a]
    start_ms = ep["start_ms"]
    sim0 = t[0] if t else 0.0
    fig, axes = plt.subplots(4, 1, figsize=(6.4, 7.6), sharex=True)
    ax = axes[0]
    ax.plot(t, [float(r["speed_mps"]) for r in a], color=colour, label="speed")
    ax.plot(t, [float(r["target_speed_mps"]) for r in a], color=MUTED, lw=1.2, ls="--", label="target")
    ax.set_ylabel("A speed (m/s)")
    ax.legend(loc="lower left", ncol=2)
    ax = axes[1]
    ax.plot(t, [float(r["accel_applied_mps2"]) for r in a], color=colour)
    ax.axhline(0.0, color=MUTED, lw=0.8)
    ax.set_ylabel("A accel (m/s²)\nbraking < 0")
    states = [r["controller_state"] for r in a]
    for i, state in enumerate(states):
        if i == 0 or state != states[i - 1]:
            ax.annotate(state, (t[i], 0.0), xytext=(2, 14 if i % 2 else -18), textcoords="offset points",
                        color=MUTED, fontsize=7)
    ax = axes[2]
    rx_path = ep["dir"] / "v2x_rx.csv"
    rx = list(csv.DictReader(open(rx_path, newline="", encoding="utf-8"))) if rx_path.exists() else []
    if rx:
        ax.plot([float(r["simulation_time_at_receive"]) for r in rx], [float(r["one_way_ms"]) for r in rx],
                "o", ms=3.5, color=colour)
        ax.set_ylabel("V2X one-way\ndelay (ms)")
    else:
        ax.text(0.5, 0.5, "no V2X messages received", transform=ax.transAxes, ha="center", color=MUTED)
        ax.set_ylabel("V2X")
    ax = axes[3]
    ch = [r for r in analyze.window(bridge, start_ms, ep["end_ms"]) if r["tap_db"] is not None]
    if ch:
        ax.plot([sim0 + (r["t_ms"] - start_ms) / 1000.0 for r in ch], [r["tap_db"] for r in ch], color=colour)
        ax.set_ylabel("DL strongest\ntap to A (dB)")
    else:
        ax.text(0.5, 0.5, "no channel record in this run", transform=ax.transAxes, ha="center", color=MUTED)
    ax.set_xlabel("simulation time (s)")
    res = ep["result"]
    fig.suptitle(f"{ep['name']}: {res['ended']}, min centre gap {res['min_centre_gap_m']} m",
                 color=INK, x=0.02, ha="left")
    fig.tight_layout()
    fig.savefig(out / f"episode-{ep['name']}.png", dpi=150)
    plt.close(fig)


def plot_gpu(series, out):
    fig, ax = plt.subplots(figsize=(6.4, 2.8))
    ax.plot([g["t"] for g in series], [g["total_us"] for g in series], color=SLOTS[0], lw=1.2)
    ax.axhline(analyze.SLOT_US, color=MUTED, ls="--", lw=1.0)
    ax.annotate("1 ms slot", (series[0]["t"], analyze.SLOT_US), xytext=(2, 3), textcoords="offset points",
                color=MUTED)
    ax.set_yscale("log")
    ax.set_xlabel("broker run time (s)")
    ax.set_ylabel("GPU H2D+kernel+D2H\n(µs, per-second avg)")
    fig.tight_layout()
    fig.savefig(out / "gpu.png", dpi=160)
    plt.close(fig)


def plot_response(table, colours, out):
    fig, ax = plt.subplots(figsize=(5.0, 3.4))
    for mode in sorted({r["mode"] for r in table}):
        rows = [r for r in table if r["mode"] == mode and r["longest_rx_gap_ms"] is not None
                and r["first_risk_s"] is not None]
        if rows:
            ax.plot([r["longest_rx_gap_ms"] for r in rows], [r["first_risk_s"] for r in rows], "o", ms=7,
                    color=colours[mode], mec=SURFACE, mew=2, label=mode)
    ax.set_xlabel("longest V2X reception gap in episode (ms)")
    ax.set_ylabel("A first WARNING/BRAKING (s)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "response.png", dpi=160)
    plt.close(fig)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path)
    ap.add_argument("--node", default="ue0")
    ap.add_argument("--max-episodes", type=int, default=6, help="per-episode figures to draw")
    args = ap.parse_args(argv)
    run = args.run.resolve()
    v2x_dir = run / "v2x" if (run / "v2x" / "episodes").exists() else run
    out = args.out or run / "v2x-analysis"
    out.mkdir(parents=True, exist_ok=True)
    episodes = analyze.load_episodes(v2x_dir)
    colours = mode_colours({ep["result"]["label"] for ep in episodes})
    bridge = analyze.load_bridge(run, args.node)
    plot_trajectories(episodes, colours, out)
    for ep in episodes[: args.max_episodes]:
        plot_episode(ep, colours[ep["result"]["label"]], bridge, out)
    broker = analyze.load_broker(run)
    if broker and broker["series"]:
        plot_gpu(broker["series"], out)
    summary = out / "episodes.csv"
    if summary.exists():
        table = []
        for row in csv.DictReader(open(summary, newline="", encoding="utf-8")):
            table.append({"mode": row["mode"],
                          "longest_rx_gap_ms": float(row["longest_rx_gap_ms"]) if row["longest_rx_gap_ms"] else None,
                          "first_risk_s": float(row["first_risk_s"]) if row["first_risk_s"] else None})
        plot_response(table, colours, out)
    print(f"figures in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
