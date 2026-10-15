"""Isaac Sim world: the layout's building and three differential-drive robots in PhysX (Isaac Sim 6.2).

The only module that imports Isaac Sim. Same interface as kinematic_world.py,
so sim.py, the robots and the referee do not change with the world. Run with
the interpreter `setup-external.sh --isaac` writes (`~/venvs/rescue/bin/rescue-python`:
EULA flag, libgomp preload, user-space X11/GL libraries).

- Scene: a ground plane, every `layout.OBSTACLES` box (walls, rubble) as a
  FixedCuboid, every hidden survivor as a 0.6 x 0.6 x 0.35 m FixedCuboid (an
  obstacle the LiDAR sees; nothing marks it as a survivor).
- Robots: built from primitives (chassis 0.40 x 0.30 x 0.12 m inside the
  0.25 m robot radius, two driven revolute wheels, a frictionless caster) and
  driven by `DifferentialController` from the commanded (v, w). The NVIDIA
  robot assets come from an internet asset server and are not used.
- LiDAR: one `Raycast` sensor per robot (PhysX scene queries), `sensors.SCAN_BEAMS`
  rays in the chassis frame from -pi, the kinematic world's convention, at
  chassis height so the robots see each other. A ray starting inside the
  robot's own chassis hits it (measured: every beam 0.05 m), so each ray starts
  `RAY_START_M` out along its direction, beyond the 0.25 m robot radius, and
  that offset is added back; nothing can be closer than the robot radius
  anyway. No-hit rays read `sensors.SCAN_MAX_RANGE_M`.
- Survivor perception: the same geometric mock as the kinematic world
  (`sensors.detect_survivors`) on the PhysX poses.
- Physics steps at sim.py's `physics_dt` with `render=False` (rendering is
  not needed for the LiDAR and costs ~8 ms per step on the GB10).
- Contacts are not counted (`contacts` reads None): PhysX contact reporting is
  not wired up.
- Views (`capture`): an overview camera above the building and a forward
  camera on each robot, rendered on demand (render ~18 ms for two cameras on
  the GB10) and written as JPEGs for the dashboard. Colours are for viewing
  only: walls by material, rubble brown, survivors orange, robots by id.
"""

from __future__ import annotations

import math

import numpy as np

import layout
import sensors

WHEEL_RADIUS_M = 0.06
WHEEL_BASE_M = 0.34
CHASSIS_M = (0.40, 0.30, 0.12)
LIDAR_HEIGHT_M = 0.10          # inside the chassis band (0.04-0.16 m) of the other robots
RAY_START_M = 0.27
SURVIVOR_BOX_M = (0.6, 0.6, 0.35)
MAX_V_MPS = 0.6
MAX_W_RPS = 1.5
WALL_COLOURS = {"concrete": (0.62, 0.62, 0.60), "brick": (0.62, 0.36, 0.27), "plasterboard": (0.86, 0.85, 0.80),
                "metal": (0.42, 0.48, 0.58), "wood": (0.55, 0.40, 0.25)}
RUBBLE_COLOUR = (0.50, 0.40, 0.28)
SURVIVOR_COLOUR = (1.0, 0.45, 0.10)
ROBOT_COLOURS = {"robot_1": (0.15, 0.45, 0.85), "robot_2": (0.10, 0.65, 0.35), "robot_3": (0.80, 0.25, 0.70)}
# focal 20 mm on a 20.955 mm aperture = 55 deg horizontal: the 40 x 24 m building fills the frame from 52 m.
OVERVIEW_CAMERA = {"position": (20.0, -19.0, 40.0), "euler_deg": (0.0, 52.0, 90.0), "resolution": (960, 540),
                   "focal_mm": 20.0}
VIEW_WARMUP_LIMIT_S = 300.0
BEACON_RADIUS_M = 0.35             # visual marker above each robot, no collider (the LiDAR ignores it)
BEACON_HEIGHT_M = 1.2
ROBOT_CAMERA = {"translation": (0.22, 0.0, 0.15), "resolution": (640, 360), "focal_mm": 12.0}


class IsaacWorld:
    name = "isaac"

    def __init__(self, survivors, robot_ids=layout.ROBOT_IDS, physics_dt: float = 1.0 / 60.0, render: bool = False,
                 views: bool = False):
        import os
        import time
        from isaacsim import SimulationApp      # must exist before any other isaacsim import
        config = {"headless": True}
        if os.environ.get("RESCUE_KIT_ROOT"):
            # Kit's caches default to the venv (`--portable`). The live gate's root, inside a user
            # namespace, cannot write there and then recompiles shaders on every render, stalling
            # the world (run 20261015T121010Z stopped at sim time 5 s). The launcher gives Kit a
            # writable root, seeded with the venv's warm cache.
            config["extra_args"] = ["--portable-root", os.environ["RESCUE_KIT_ROOT"]]
        self.app = SimulationApp(config)
        from isaacsim.core.api import World
        from isaacsim.core.api.objects import FixedCuboid, GroundPlane
        from isaacsim.core.version import get_version
        from isaacsim.robot.wheeled_robots.controllers.differential_controller import DifferentialController
        from isaacsim.robot.wheeled_robots.robots import WheeledRobot
        from isaacsim.sensors.experimental.physics import Raycast, RaycastSensor
        from pxr import Gf, PhysxSchema, UsdGeom, UsdPhysics, UsdShade

        self.version = get_version()[0]
        self.survivors = [tuple(s) for s in survivors]
        self.robot_ids = tuple(robot_ids)
        self.render = render
        self.physics_dt = physics_dt
        self.world = World(stage_units_in_meters=1.0, physics_dt=physics_dt, rendering_dt=physics_dt)
        stage = self.world.stage
        self.world.scene.add(GroundPlane(prim_path="/World/ground", size=200.0, z_position=0.0))
        for b in layout.OBSTACLES:
            sx, sy, sz = b.size
            cx, cy = b.centre
            colour = RUBBLE_COLOUR if b.name.startswith("rubble_") else WALL_COLOURS[b.material]
            self.world.scene.add(FixedCuboid(prim_path=f"/World/obstacles/{b.name}", name=b.name,
                                             position=np.array([cx, cy, b.z0 + sz / 2]),
                                             scale=np.array([sx, sy, sz]), size=1.0, color=np.array(colour)))
        for index, (x, y) in enumerate(self.survivors):
            sx, sy, sz = SURVIVOR_BOX_M
            self.world.scene.add(FixedCuboid(prim_path=f"/World/people/p{index}", name=f"p{index}",
                                             position=np.array([x, y, sz / 2]), scale=np.array([sx, sy, sz]),
                                             size=1.0, color=np.array(SURVIVOR_COLOUR)))

        def material(path, friction):
            UsdShade.Material.Define(stage, path)
            api = UsdPhysics.MaterialAPI.Apply(stage.GetPrimAtPath(path))
            api.CreateStaticFrictionAttr(friction)
            api.CreateDynamicFrictionAttr(friction)
            api.CreateRestitutionAttr(0.0)
            return UsdShade.Material(stage.GetPrimAtPath(path))

        caster_mat = material("/World/materials/caster", 0.0)
        wheel_mat = material("/World/materials/wheel", 1.0)

        def bind(prim, mat):
            UsdShade.MaterialBindingAPI.Apply(prim).Bind(mat, UsdShade.Tokens.weakerThanDescendants, "physics")

        def build(root, x, y, yaw):
            r = UsdGeom.Xform.Define(stage, root)
            r.AddTranslateOp().Set(Gf.Vec3d(x, y, WHEEL_RADIUS_M))
            r.AddOrientOp().Set(Gf.Quatf(math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)))
            UsdPhysics.ArticulationRootAPI.Apply(r.GetPrim())
            PhysxSchema.PhysxArticulationAPI.Apply(r.GetPrim()).CreateEnabledSelfCollisionsAttr(False)
            chassis = UsdGeom.Xform.Define(stage, f"{root}/chassis")
            UsdPhysics.RigidBodyAPI.Apply(chassis.GetPrim())
            UsdPhysics.MassAPI.Apply(chassis.GetPrim()).CreateMassAttr(4.0)
            body = UsdGeom.Cube.Define(stage, f"{root}/chassis/body")
            body.CreateSizeAttr(1.0)
            body.AddTranslateOp().Set(Gf.Vec3d(0, 0, CHASSIS_M[2] / 2 - 0.02))
            body.AddScaleOp().Set(Gf.Vec3f(*CHASSIS_M))
            body.CreateDisplayColorAttr([Gf.Vec3f(*ROBOT_COLOURS.get(root.rsplit("/", 1)[-1], (0.8, 0.8, 0.2)))])
            UsdPhysics.CollisionAPI.Apply(body.GetPrim())
            beacon = UsdGeom.Sphere.Define(stage, f"{root}/chassis/beacon")
            beacon.CreateRadiusAttr(BEACON_RADIUS_M)
            beacon.AddTranslateOp().Set(Gf.Vec3d(0, 0, BEACON_HEIGHT_M))
            beacon.CreateDisplayColorAttr([Gf.Vec3f(*ROBOT_COLOURS.get(root.rsplit("/", 1)[-1], (0.8, 0.8, 0.2)))])
            caster = UsdGeom.Sphere.Define(stage, f"{root}/chassis/caster")
            caster.CreateRadiusAttr(0.03)
            caster.AddTranslateOp().Set(Gf.Vec3f(0.15, 0, -(WHEEL_RADIUS_M - 0.03)))
            UsdPhysics.CollisionAPI.Apply(caster.GetPrim())
            bind(caster.GetPrim(), caster_mat)
            for side, sign in (("left", 1), ("right", -1)):
                wheel_path = f"{root}/{side}_wheel"
                wheel = UsdGeom.Xform.Define(stage, wheel_path)
                wheel.AddTranslateOp().Set(Gf.Vec3d(0, sign * WHEEL_BASE_M / 2, 0))
                UsdPhysics.RigidBodyAPI.Apply(wheel.GetPrim())
                UsdPhysics.MassAPI.Apply(wheel.GetPrim()).CreateMassAttr(0.3)
                cyl = UsdGeom.Cylinder.Define(stage, f"{wheel_path}/geom")
                cyl.CreateRadiusAttr(WHEEL_RADIUS_M)
                cyl.CreateHeightAttr(0.04)
                cyl.CreateAxisAttr("Y")
                UsdPhysics.CollisionAPI.Apply(cyl.GetPrim())
                bind(cyl.GetPrim(), wheel_mat)
                joint = UsdPhysics.RevoluteJoint.Define(stage, f"{root}/{side}_wheel_joint")
                joint.CreateBody0Rel().SetTargets([f"{root}/chassis"])
                joint.CreateBody1Rel().SetTargets([wheel_path])
                joint.CreateAxisAttr("Y")
                joint.CreateLocalPos0Attr(Gf.Vec3f(0, sign * WHEEL_BASE_M / 2, 0))
                joint.CreateLocalPos1Attr(Gf.Vec3f(0, 0, 0))
                joint.CreateLocalRot0Attr(Gf.Quatf(1, 0, 0, 0))
                joint.CreateLocalRot1Attr(Gf.Quatf(1, 0, 0, 0))
                drive = UsdPhysics.DriveAPI.Apply(joint.GetPrim(), "angular")
                drive.CreateTypeAttr("force")
                drive.CreateStiffnessAttr(0.0)
                drive.CreateDampingAttr(1.0e4)
                drive.CreateMaxForceAttr(100.0)
                drive.CreateTargetVelocityAttr(0.0)

        self.robots, self.lidars = {}, {}
        angles = sensors.SCAN_ANGLE_MIN + sensors.SCAN_ANGLE_INCREMENT * np.arange(sensors.SCAN_BEAMS)
        directions = np.stack([np.cos(angles), np.sin(angles), np.zeros_like(angles)], 1)
        for rid in self.robot_ids:
            root = f"/World/{rid}"
            build(root, *layout.SPAWN_POSES[rid])
            self.robots[rid] = self.world.scene.add(WheeledRobot(
                prim_path=root, name=rid, wheel_dof_names=["left_wheel_joint", "right_wheel_joint"],
                create_robot=False))
            raycast = Raycast.create(f"{root}/chassis/lidar",
                                     translations=[[0.0, 0.0, LIDAR_HEIGHT_M - WHEEL_RADIUS_M]],
                                     min_range=0.0, max_range=sensors.SCAN_MAX_RANGE_M - RAY_START_M,
                                     ray_origins=RAY_START_M * directions, ray_directions=directions)
            self.lidars[rid] = RaycastSensor(raycast)
        self.world.reset()
        self.cameras = {}
        if views:
            # Cameras are added after reset: created before it, the robot cameras came up pointing at the sky.
            from isaacsim.core.utils.rotations import euler_angles_to_quat
            from isaacsim.sensors.camera import Camera
            from pxr import Sdf, UsdLux
            UsdLux.DomeLight.Define(stage, Sdf.Path("/World/lights/dome")).CreateIntensityAttr(350.0)
            sun = UsdLux.DistantLight.Define(stage, Sdf.Path("/World/lights/sun"))
            sun.CreateIntensityAttr(900.0)
            sun.AddRotateXYZOp().Set(Gf.Vec3f(-40.0, 20.0, 0.0))
            o = OVERVIEW_CAMERA
            self.cameras["overview"] = Camera(prim_path="/World/views/overview", position=np.array(o["position"]),
                                              orientation=euler_angles_to_quat(np.array(o["euler_deg"]), degrees=True),
                                              resolution=o["resolution"])
            for rid in self.robot_ids:
                self.cameras[rid] = Camera(prim_path=f"/World/{rid}/chassis/view",
                                           translation=np.array(ROBOT_CAMERA["translation"]),
                                           orientation=euler_angles_to_quat(np.zeros(3), degrees=True),
                                           resolution=ROBOT_CAMERA["resolution"])
            ready_at = None
            for name, camera in self.cameras.items():
                camera.initialize()
                focal = OVERVIEW_CAMERA["focal_mm"] if name == "overview" else ROBOT_CAMERA["focal_mm"]
                UsdGeom.Camera(stage.GetPrimAtPath(camera.prim_path)).GetFocalLengthAttr().Set(focal)
                UsdGeom.Camera(stage.GetPrimAtPath(camera.prim_path)).GetHorizontalApertureAttr().Set(20.955)
            # Warm up before the episode clock starts: the first renders compile shaders and the
            # annotators return nothing for a few frames. Bounded; logged; never inside the episode.
            # Paced: rendering back to back while shaders compile exhausted RTX resource descriptors
            # (run 20261015T122241Z: `Out of resource descriptors!`). Views are display only, so a
            # warm-up that does not finish turns them off for the run, logged, instead of stalling it.
            started = time.monotonic()
            while time.monotonic() - started < VIEW_WARMUP_LIMIT_S:
                self.world.render()
                rgba = [c.get_rgba() for c in self.cameras.values()]
                if all(a is not None and getattr(a, "size", 0) for a in rgba):
                    ready_at = time.monotonic() - started
                    break
                time.sleep(0.1)
            self.view_warmup_s = None if ready_at is None else round(ready_at, 2)
            if ready_at is None:
                self.cameras = {}
                print(f"event=isaac_views_disabled reason=warmup_timeout limit_s={VIEW_WARMUP_LIMIT_S}", flush=True)
            else:
                print(f"event=isaac_views_ready warmup_s={self.view_warmup_s}", flush=True)
        self.controller = DifferentialController(name="diff", wheel_radius=WHEEL_RADIUS_M, wheel_base=WHEEL_BASE_M)
        self.cmd = {rid: (0.0, 0.0) for rid in self.robot_ids}
        self.contacts = {rid: None for rid in self.robot_ids}
        self.distance_m = {rid: 0.0 for rid in self.robot_ids}
        self.sim_time_s = 0.0
        self._last_xy = {rid: self.get_pose(rid)[:2] for rid in self.robot_ids}

    def close(self) -> None:
        self.app.close()

    def set_cmd(self, robot_id: str, v: float, w: float) -> None:
        self.cmd[robot_id] = (max(-MAX_V_MPS, min(MAX_V_MPS, v)), max(-MAX_W_RPS, min(MAX_W_RPS, w)))

    def step(self, dt: float) -> None:
        for rid, robot in self.robots.items():
            robot.apply_wheel_actions(self.controller.forward(np.array(self.cmd[rid])))
        self.world.step(render=self.render)
        self.sim_time_s += self.physics_dt
        for rid in self.robot_ids:
            x, y, _ = self.get_pose(rid)
            lx, ly = self._last_xy[rid]
            self.distance_m[rid] += math.hypot(x - lx, y - ly)
            self._last_xy[rid] = (x, y)

    def get_pose(self, robot_id: str) -> tuple[float, float, float]:
        position, (w, x, y, z) = self.robots[robot_id].get_world_pose()
        yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
        return float(position[0]), float(position[1]), yaw

    def get_velocity(self, robot_id: str) -> tuple[float, float, float]:
        v = self.robots[robot_id].get_linear_velocity()
        w = self.robots[robot_id].get_angular_velocity()
        return float(v[0]), float(v[1]), float(w[2])

    def scan(self, robot_id: str) -> tuple[np.ndarray, float, float, float]:
        depths = np.asarray(self.lidars[robot_id].get_data()["depths"], dtype=np.float32).reshape(-1)
        rmax = sensors.SCAN_MAX_RANGE_M
        depths = depths + RAY_START_M
        depths = np.where((depths <= RAY_START_M) | ~np.isfinite(depths) | (depths > rmax), rmax, depths)
        return depths, sensors.SCAN_ANGLE_MIN, sensors.SCAN_ANGLE_INCREMENT, rmax

    def capture(self, directory) -> dict[str, float]:
        """Render once and write <view>.jpg for every camera (atomic rename); returns ms per stage."""

        if not self.cameras:
            return {}

        import os
        import time
        from PIL import Image
        t0 = time.perf_counter()
        self.world.render()
        t1 = time.perf_counter()
        for name, camera in self.cameras.items():
            rgba = camera.get_rgba()
            if rgba is None or rgba.size == 0:
                continue
            path = directory / f"{name}.jpg"
            tmp = directory / f".{name}.jpg.tmp"
            Image.fromarray(np.asarray(rgba)[..., :3].astype(np.uint8)).save(tmp, format="JPEG", quality=75)
            os.replace(tmp, path)
        return {"render_ms": (t1 - t0) * 1000, "encode_ms": (time.perf_counter() - t1) * 1000}

    def detections(self, robot_id: str) -> list[dict]:
        discs = [(*self.get_pose(o)[:2], layout.ROBOT_RADIUS_M) for o in self.robot_ids if o != robot_id]
        return sensors.detect_survivors(self.get_pose(robot_id), self.survivors, discs)
