#!/usr/bin/env python3
"""Run one episode: SUMO + vehicle A's controller, paced to the wall clock.

Per 50 ms step:

1. read A and B from SUMO;
2. drain the in-vehicle relay socket (V2X messages the UE client received
   over the 5G link, `messages.pack_relay` frames) and keep the newest;
3. local sensor: B's true state if B is in line of sight (layout.BLOCKS)
   and within `--sensor-range-m`;
4. controller -> acceleration; SUMO applies it (`sumo_world.SumoWorld.step`);
5. publish positions on `--positions-endpoint` (Sionna bridge: A's antenna as
   radio node `--a-node`; roadside server: both vehicles).

Simulation time is advanced to match elapsed wall time and nobody waits for
anybody: a late or lost V2X message is an event the controller sees, never a
stall. If a step itself runs late, the loop records it (`late_steps`,
`max_lag_ms`, `rtf` in the result) and catches up without skipping control.

Outputs in `--out`: vehicles.csv (one row per step and vehicle), v2x_rx.csv
(one row per received message), result.json (outcome and timing).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pathlib
import socket
import statistics
import sys
import time

import layout
import messages
from controller import Controller, Params, select_estimate
import sumo_world

VEHICLE_FIELDS = [
    "simulation_time", "wall_time", "vehicle_id", "position_x", "position_y", "speed_mps",
    "accel_cmd_mps2", "accel_applied_mps2", "throttle", "brake", "controller_state",
    "target_speed_mps", "info", "info_age_s", "d_a_m", "d_b_m", "t_a_s", "t_b_s", "b_visible", "reason",
]
RX_FIELDS = [
    "sequence", "send_wall_time_ns", "ue_receive_wall_time_ns", "controller_receive_wall_time_ns",
    "one_way_ms", "message_simulation_time", "simulation_time_at_receive", "controller_state_at_receive",
]


def obb_overlap(c1, yaw1, c2, yaw2, length=layout.VEHICLE_LENGTH_M, width=layout.VEHICLE_WIDTH_M) -> bool:
    """Separating-axis test for two vehicle footprints (centre, yaw)."""

    def corners(c, yaw):
        ux, uy = math.cos(yaw), math.sin(yaw)
        hl, hw = 0.5 * length, 0.5 * width
        return [(c[0] + sx * hl * ux - sy * hw * uy, c[1] + sx * hl * uy + sy * hw * ux)
                for sx, sy in ((1, 1), (1, -1), (-1, -1), (-1, 1))]

    p, q = corners(c1, yaw1), corners(c2, yaw2)
    for yaw in (yaw1, yaw2):
        for ax in ((math.cos(yaw), math.sin(yaw)), (-math.sin(yaw), math.cos(yaw))):
            pp = [x * ax[0] + y * ax[1] for x, y in p]
            qq = [x * ax[0] + y * ax[1] for x, y in q]
            if max(pp) < min(qq) or max(qq) < min(pp):
                return False
    return True


def sumo_centre(state) -> tuple[float, float]:
    """SUMO reports the front bumper; the footprint centre is half a length behind."""

    yaw = state.orientation[2]
    half = 0.5 * layout.VEHICLE_LENGTH_M
    return (state.position[0] - half * math.cos(yaw), state.position[1] - half * math.sin(yaw))


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q / 100.0 * (len(ordered) - 1))))]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--v2x-socket", type=pathlib.Path,
                    help="unix datagram socket to bind for relayed V2X messages (unset: no V2X input)")
    ap.add_argument("--positions-endpoint", help="ZMQ PUB endpoint to bind (Sionna bridge + roadside)")
    ap.add_argument("--a-node", default="ue0", help="radio node id of A's UE in the Sionna scenario")
    ap.add_argument("--hold-until-file", type=pathlib.Path,
                    help="publish the start positions and hold time 0 until this file exists")
    ap.add_argument("--step-s", type=float, default=0.05)
    ap.add_argument("--time-limit-s", type=float, default=30.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--a-start-y", type=float, default=layout.A_START_M[1])
    ap.add_argument("--b-start-x", type=float, default=layout.B_START_M[0])
    ap.add_argument("--a-speed", type=float, default=layout.CRUISE_SPEED_MPS)
    ap.add_argument("--b-speed", type=float, default=layout.CRUISE_SPEED_MPS)
    ap.add_argument("--fallback", choices=("cautious", "none"), default="cautious")
    ap.add_argument("--no-v2x", action="store_true", help="controller ignores V2X (local sensor only)")
    ap.add_argument("--stale-after-s", type=float, default=Params.stale_after_s)
    ap.add_argument("--sensor-range-m", type=float, default=50.0)
    ap.add_argument("--lockstep", action="store_true",
                    help="no wall-clock pacing (offline tests only; recorded in the result)")
    ap.add_argument("--label", default="", help="free text copied into result.json")
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    params = Params(cruise_speed_mps=args.a_speed, fallback=args.fallback, use_v2x=not args.no_v2x,
                    stale_after_s=args.stale_after_s)
    start = sumo_world.Start(a_position=(layout.A_LANE_X_M, args.a_start_y),
                             b_position=(args.b_start_x, layout.B_LANE_Y_M),
                             a_speed_mps=args.a_speed, b_speed_mps=args.b_speed)
    world = sumo_world.SumoWorld(args.out / "sumo", step_s=args.step_s, seed=args.seed, start=start,
                                 accel_max=params.accel_max, decel_max=params.decel_max)
    controller = Controller(layout.CONFLICT_POINT_M, layout.STOP_LINE_GAP_M, params)

    rx_sock = None
    if args.v2x_socket is not None:
        args.v2x_socket.parent.mkdir(parents=True, exist_ok=True)
        if args.v2x_socket.exists():
            args.v2x_socket.unlink()
        rx_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        rx_sock.bind(str(args.v2x_socket))
        rx_sock.setblocking(False)

    pub = None
    if args.positions_endpoint:
        import zmq

        pub = zmq.Context.instance().socket(zmq.PUB)
        pub.setsockopt(zmq.SNDHWM, 10)
        pub.setsockopt(zmq.LINGER, 0)
        if args.positions_endpoint.startswith("ipc://"):
            pathlib.Path(args.positions_endpoint[len("ipc://"):]).parent.mkdir(parents=True, exist_ok=True)
        pub.bind(args.positions_endpoint)

    def publish(a, b) -> None:
        if pub is None:
            return
        vehicles = {
            s.vehicle_id: {
                "position_m": [round(v, 3) for v in s.position],
                "velocity_mps": [round(v, 3) for v in s.velocity],
                "yaw_rad": round(s.orientation[2], 4),
                "speed_mps": round(s.speed_mps, 3),
            }
            for s in (a, b)
        }
        antenna = (a.position[0], a.position[1], layout.ANTENNA_HEIGHT_M)
        pub.send_string(messages.positions_message(
            t_unix_ms=time.time_ns() // 1_000_000,
            simulation_time=a.simulation_time,
            radio_nodes={args.a_node: (antenna, a.velocity)},
            vehicles=vehicles,
        ))

    def drain(on_message) -> int:
        count = 0
        if rx_sock is None:
            return 0
        while True:
            try:
                data = rx_sock.recv(65536)
            except BlockingIOError:
                return count
            count += 1
            relay_ns = time.time_ns()
            try:
                ue_ns, payload = messages.unpack_relay(data)
                message = messages.decode(payload)
            except ValueError as exc:
                print(f"drop malformed relay frame: {exc}", file=sys.stderr)
                continue
            on_message(message, ue_ns, relay_ns)

    held_messages = 0
    hold_wall_s = 0.0
    if args.hold_until_file is not None:
        hold_started = time.monotonic()
        while not args.hold_until_file.exists():
            publish(world.state("A"), world.state("B"))
            held_messages += drain(lambda *unused: None)
            time.sleep(args.step_s)
        hold_wall_s = time.monotonic() - hold_started

    veh_file = open(args.out / "vehicles.csv", "w", newline="", encoding="utf-8")
    rx_file = open(args.out / "v2x_rx.csv", "w", newline="", encoding="utf-8")
    veh_csv = csv.DictWriter(veh_file, fieldnames=VEHICLE_FIELDS)
    rx_csv = csv.DictWriter(rx_file, fieldnames=RX_FIELDS)
    veh_csv.writeheader()
    rx_csv.writeheader()

    latest = None
    rx_one_way_ms: list[float] = []
    rx_sequences: set[int] = set()
    out_of_order = 0
    first_risk = None
    first_brake = None
    min_centre_gap = math.inf
    collision_time = None
    a_cross = b_cross = None
    late_steps = 0
    max_lag_s = 0.0
    sim0 = world.simulation_time
    wall0 = time.monotonic()
    wall0_unix_ns = time.time_ns()
    ended = "time_limit"
    step = 0

    while True:
        if not args.lockstep:
            due = wall0 + step * args.step_s
            now = time.monotonic()
            if now < due:
                time.sleep(due - now)
            else:
                lag = now - due
                max_lag_s = max(max_lag_s, lag)
                if lag > args.step_s:
                    late_steps += 1
        a, b = world.state("A"), world.state("B")
        sim_now = a.simulation_time

        def on_message(message, ue_ns, relay_ns):
            nonlocal latest, out_of_order
            one_way = (ue_ns - message.send_wall_time_ns) / 1e6
            rx_one_way_ms.append(one_way)
            rx_sequences.add(message.sequence)
            if latest is not None and message.sequence <= latest.sequence:
                out_of_order += 1
            else:
                latest = message
            rx_csv.writerow({
                "sequence": message.sequence,
                "send_wall_time_ns": message.send_wall_time_ns,
                "ue_receive_wall_time_ns": ue_ns,
                "controller_receive_wall_time_ns": relay_ns,
                "one_way_ms": round(one_way, 3),
                "message_simulation_time": message.simulation_time,
                "simulation_time_at_receive": round(sim_now, 3),
                "controller_state_at_receive": controller.state,
            })

        drain(on_message)
        b_xy = (b.position[0], b.position[1])
        visible = (layout.distance(a.position[:2], b_xy) <= args.sensor_range_m
                   and layout.line_of_sight(a.position[:2], b_xy))
        sensor = (b_xy, b.velocity[:2]) if visible else None
        probe = layout.CROSS_APPROACH_PROBE_M
        approach_in_view = (layout.distance(a.position[:2], probe) <= args.sensor_range_m
                            and layout.line_of_sight(a.position[:2], probe))
        estimate, info = select_estimate(now_sim=sim_now, sensor=sensor, approach_in_view=approach_in_view,
                                         v2x=latest, params=params)
        out = controller.step(a, estimate, args.step_s, info=info)
        publish(a, b)

        if out.state in ("WARNING", "BRAKING") and first_risk is None:
            first_risk = sim_now
        if out.state == "BRAKING" and first_brake is None:
            first_brake = sim_now
        ca, cb = sumo_centre(a), sumo_centre(b)
        min_centre_gap = min(min_centre_gap, layout.distance(ca, cb))
        if collision_time is None and obb_overlap(ca, a.orientation[2], cb, b.orientation[2]):
            collision_time = sim_now
        if a_cross is None and a.position[1] - layout.VEHICLE_LENGTH_M > layout.CONFLICT_POINT_M[1]:
            a_cross = sim_now
        if b_cross is None and b.position[0] + layout.VEHICLE_LENGTH_M < layout.CONFLICT_POINT_M[0]:
            b_cross = sim_now

        applied = world.step(out.accel_mps2)
        wall_t = time.time()
        for s in (a, b):
            row = {
                "simulation_time": round(sim_now, 3), "wall_time": round(wall_t, 4), "vehicle_id": s.vehicle_id,
                "position_x": round(s.position[0], 3), "position_y": round(s.position[1], 3),
                "speed_mps": round(s.speed_mps, 3),
            }
            if s.vehicle_id == "A":
                row.update({
                    "accel_cmd_mps2": round(out.accel_mps2, 3), "accel_applied_mps2": round(applied, 3),
                    "throttle": round(out.throttle, 3), "brake": round(out.brake, 3),
                    "controller_state": out.state, "target_speed_mps": round(out.target_speed_mps, 3),
                    "info": out.info,
                    "info_age_s": "" if out.info_age_s is None else round(out.info_age_s, 3),
                    "d_a_m": round(out.d_a_m, 3),
                    "d_b_m": "" if out.d_b_m is None else round(out.d_b_m, 3),
                    "t_a_s": "" if out.t_a_s is None else round(out.t_a_s, 3),
                    "t_b_s": "" if out.t_b_s is None else round(out.t_b_s, 3),
                    "b_visible": int(visible), "reason": out.reason,
                })
            veh_csv.writerow(row)
        # Flushed every step: the dashboard (web/server.py) tails these files live.
        veh_file.flush()
        rx_file.flush()
        step += 1
        if collision_time is not None:
            ended = "collision"
            break
        if a_cross is not None and b_cross is not None:
            ended = "both_crossed"
            break
        if sim_now - sim0 >= args.time_limit_s:
            break

    wall_elapsed = time.monotonic() - wall0
    sim_elapsed = world.simulation_time - sim0
    world.close()
    veh_file.close()
    rx_file.close()
    if rx_sock is not None:
        rx_sock.close()
        args.v2x_socket.unlink(missing_ok=True)
    if pub is not None:
        pub.close(linger=0)

    result = {
        "label": args.label,
        "sumo_version": sumo_world.sumo_version(),
        "config": {k: (str(v) if isinstance(v, pathlib.Path) else v) for k, v in vars(args).items()},
        "params": vars(params),
        "start_unix_ns": wall0_unix_ns,
        "ended": ended,
        "collision": collision_time is not None,
        "collision_sim_time_s": collision_time,
        "min_centre_gap_m": round(min_centre_gap, 3),
        "first_risk_sim_time_s": first_risk,
        "first_brake_sim_time_s": first_brake,
        "a_crossed_sim_time_s": a_cross,
        "b_crossed_sim_time_s": b_cross,
        "order": (None if a_cross is None or b_cross is None else ("A_first" if a_cross < b_cross else "B_first")),
        "v2x": {
            "input": rx_sock is not None,
            "used": params.use_v2x,
            "messages_received": len(rx_one_way_ms),
            "unique_sequences": len(rx_sequences),
            "out_of_order": out_of_order,
            "one_way_ms": {
                "p50": percentile(rx_one_way_ms, 50), "p95": percentile(rx_one_way_ms, 95),
                "max": max(rx_one_way_ms) if rx_one_way_ms else None,
                "mean": statistics.fmean(rx_one_way_ms) if rx_one_way_ms else None,
            },
            "discarded_while_holding": held_messages,
        },
        "timing": {
            "lockstep": args.lockstep,
            "hold_wall_s": round(hold_wall_s, 3),
            "steps": step,
            "sim_elapsed_s": round(sim_elapsed, 3),
            "wall_elapsed_s": round(wall_elapsed, 3),
            "rtf": round(sim_elapsed / wall_elapsed, 4) if wall_elapsed > 0 else None,
            "late_steps": late_steps,
            "max_lag_ms": round(max_lag_s * 1000.0, 3),
        },
    }
    (args.out / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("label", "ended", "collision", "min_centre_gap_m",
                                             "first_brake_sim_time_s", "order")}
                     | {"rx": result["v2x"]["messages_received"], "rtf": result["timing"]["rtf"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
