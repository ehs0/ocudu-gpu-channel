#!/usr/bin/env python3
"""Isaac Sim side of the robot/channel demo: build the room, drive two robots.

Run with the Isaac Sim interpreter, not the Sionna one:

    OMNI_KIT_ACCEPT_EULA=YES \
    /home/hyunsoo/OCUDU/venvs/isaacsim/bin/python scripts/isaac/room_robots.py \
        --room examples/sionna/scenes/room_table/room.json

The room geometry is *read*, never re-declared. `room.json` is written by
`scripts/sionna_rt/build_room_scene.py` alongside the Mitsuba scene that
Sionna traces, so the colliders here and the radio surfaces there are the
same boxes by construction. Re-typing the dimensions is how the physics and
the propagation quietly stop describing the same room.

Poses go out on a ZMQ PUB socket that `run_bridge.py --position-endpoint`
subscribes to. The two simulators never block on each other: Isaac publishes
at its physics rate and the bridge takes whatever the newest frame is.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib

# isaacsim must be imported and the app started before any omni.* import,
# because starting the app is what loads those extensions.
from isaacsim import SimulationApp

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--room", type=pathlib.Path, required=True,
                    help="room.json written by build_room_scene.py")
parser.add_argument("--publish-endpoint", default="tcp://127.0.0.1:5601")
parser.add_argument("--antenna-height-m", type=float, default=0.4,
                    help="UE antenna height above the robot's base origin")
parser.add_argument("--orbit-radius-m", type=float, default=2.2)
parser.add_argument("--orbit-rate-rad-s", type=float, default=0.6)
parser.add_argument("--headless", action="store_true")
args = parser.parse_args()

simulation_app = SimulationApp({"headless": args.headless})

import zmq  # noqa: E402  (after SimulationApp, per Isaac's import rules)
from isaacsim.core.api import World  # noqa: E402
from isaacsim.core.api.objects import DynamicCuboid, FixedCuboid  # noqa: E402


def build_room(world: World, room: dict) -> None:
    """Recreate the Sionna solids as static colliders."""

    for item in room["boxes"]:
        world.scene.add(FixedCuboid(
            prim_path=f"/World/{item['id']}",
            name=item["id"],
            position=item["center"],
            scale=item["size"],
        ))


def main() -> None:
    room = json.loads(args.room.read_text())

    context = zmq.Context.instance()
    publisher = context.socket(zmq.PUB)
    # HWM 1: a subscriber that falls behind should lose old poses, not
    # accumulate them. The bridge conflates on its side too.
    publisher.setsockopt(zmq.SNDHWM, 1)
    publisher.bind(args.publish_endpoint)

    world = World(stage_units_in_meters=1.0)
    world.scene.add_default_ground_plane()
    build_room(world, room)

    # Plain cuboids stand in for robots here. Swapping in a wheeled asset
    # changes only how the pose is produced -- everything downstream reads
    # `get_world_pose()`, so the channel side is unaffected.
    robots = {}
    for index, node_id in enumerate(("ue0", "ue1")):
        angle = index * math.pi
        robots[node_id] = world.scene.add(DynamicCuboid(
            prim_path=f"/World/robot_{node_id}",
            name=f"robot_{node_id}",
            position=[args.orbit_radius_m * math.cos(angle),
                      args.orbit_radius_m * math.sin(angle),
                      0.15],
            scale=[0.3, 0.3, 0.3],
            mass=5.0,
        ))

    world.reset()
    print(f"[isaac] publishing poses on {args.publish_endpoint}", flush=True)

    step = 0
    physics_dt = world.get_physics_dt()
    while simulation_app.is_running():
        elapsed = step * physics_dt
        # A scripted orbit around the table: reproducible, and it crosses the
        # blocked sector behind the table where the serving cell flips.
        for index, (node_id, robot) in enumerate(robots.items()):
            angle = args.orbit_rate_rad_s * elapsed + index * math.pi
            robot.set_world_pose(position=[
                args.orbit_radius_m * math.cos(angle),
                args.orbit_radius_m * math.sin(angle),
                0.15,
            ])

        world.step(render=not args.headless)

        frame = {}
        for node_id, robot in robots.items():
            position, _ = robot.get_world_pose()
            velocity = robot.get_linear_velocity()
            frame[node_id] = {
                # float() matters: Mitsuba's Point3f rejects numpy scalars,
                # and these arrive as float32 from Isaac.
                "position_m": [float(position[0]), float(position[1]),
                               float(position[2]) + args.antenna_height_m],
                "velocity_mps": [float(velocity[0]), float(velocity[1]), float(velocity[2])],
            }
        publisher.send_string(json.dumps(frame))
        step += 1

    simulation_app.close()


if __name__ == "__main__":
    main()
