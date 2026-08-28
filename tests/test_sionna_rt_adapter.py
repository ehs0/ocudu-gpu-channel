#!/usr/bin/env python3
"""Dependency-free tests for the Sionna CIR → runtime TDL adapter."""

from __future__ import annotations

import cmath
import math
import pathlib
import sys
import tempfile
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "sionna_rt"))

from channel_adapter import (  # noqa: E402
    LaneProfile,
    MatrixProfile,
    Ray,
    Tap,
    ZmqControlClient,
    channel_status,
    control_link_id,
    make_profile_swap,
    make_matrix_profile_swap,
    rays_to_taps,
)
from run_bridge import (  # noqa: E402
    CAR_SPEED_MPS,
    CROSSTALK_LINKS,
    DEFAULT_DOWNLINK_FREQUENCY_HZ,
    DEFAULT_GNB_HEIGHT_M,
    DEFAULT_ROAD_WIDTH_M,
    DEFAULT_UPLINK_FREQUENCY_HZ,
    DOWNLINK_LINKS,
    LINKS,
    MODEL_ID,
    Motion,
    ONE_GNB_ONE_UE_CROSSTALK_LINKS,
    ONE_GNB_ONE_UE_DOWNLINK_LINKS,
    ONE_GNB_ONE_UE_LINKS,
    ONE_GNB_ONE_UE_NODE_IDS,
    ONE_GNB_ONE_UE_UPLINK_LINKS,
    PEDESTRIAN_SPEED_MPS,
    SionnaScenario,
    UPLINK_LINKS,
    configured_motion,
    effective_scenario,
    link_layout,
    load_scenario_config,
    parse_args,
    prepare_scene_with_simple_road,
    scenario_environment,
    scene_geometry,
    write_rectangle_ply,
)


class AdapterTests(unittest.TestCase):
    def test_control_timeout_names_endpoint_and_missing_broker(self) -> None:
        class FakeAgain(Exception):
            pass

        class FakeZmq:
            Again = FakeAgain

        class FakeSocket:
            def send_string(self, _payload: str) -> None:
                return

            def recv_string(self) -> str:
                raise FakeAgain()

        client = object.__new__(ZmqControlClient)
        client._zmq = FakeZmq()  # type: ignore[attr-defined]
        client._socket = FakeSocket()  # type: ignore[attr-defined]
        client._endpoint = "tcp://127.0.0.1:5559"  # type: ignore[attr-defined]
        client._timeout_ms = 5000  # type: ignore[attr-defined]
        with self.assertRaisesRegex(
            RuntimeError,
            "control request 'batch_begin' timed out.*start ocudu-gpu-channel",
        ):
            client.request({"type": "batch_begin", "id": "sionna-0"})

    def test_two_gnb_two_ue_graph_and_horizontal_motion(self) -> None:
        self.assertEqual(len(LINKS), 10)
        self.assertEqual(len(set(LINKS)), 10)
        self.assertEqual(len(DOWNLINK_LINKS), 4)
        self.assertEqual(len(UPLINK_LINKS), 4)
        self.assertTrue(all(source.startswith("gnb") for source, _ in DOWNLINK_LINKS))
        self.assertTrue(all(source.startswith("ue") for source, _ in UPLINK_LINKS))
        self.assertEqual(CROSSTALK_LINKS, (("ue0", "ue1"), ("ue1", "ue0")))
        self.assertEqual(MODEL_ID, "sionna_rt")
        self.assertEqual(
            Motion((1.0, 2.0, 3.0), (0.5, 0.0, 0.0)).position_at(4.0),
            (3.0, 2.0, 3.0),
        )

    def test_one_gnb_one_ue_layout_has_only_bidirectional_serving_links(self) -> None:
        args = parse_args(["--layout", "1x1"])
        self.assertEqual(
            link_layout(args.layout),
            (
                ONE_GNB_ONE_UE_NODE_IDS,
                ONE_GNB_ONE_UE_DOWNLINK_LINKS,
                ONE_GNB_ONE_UE_UPLINK_LINKS,
                ONE_GNB_ONE_UE_CROSSTALK_LINKS,
                ONE_GNB_ONE_UE_LINKS,
            ),
        )
        self.assertEqual(set(configured_motion(args)), {"gnb0", "ue0"})
        environment = scenario_environment(args)
        self.assertEqual(environment["layout"], "1x1")
        self.assertEqual(environment["link_count"], 2)
        self.assertEqual(
            environment["link_groups"],
            {"downlink": 1, "uplink": 1, "ue_crosstalk": 0},
        )
        self.assertEqual(set(environment["nodes"]), {"gnb0", "ue0"})

    def test_car_and_pedestrian_defaults_follow_bounded_road_routes(self) -> None:
        args = parse_args([])
        self.assertAlmostEqual(args.ue0_velocity[0], CAR_SPEED_MPS)
        self.assertAlmostEqual(abs(args.ue1_velocity[0]), PEDESTRIAN_SPEED_MPS)
        self.assertEqual(args.road_width_m, DEFAULT_ROAD_WIDTH_M)
        self.assertEqual(args.gnb_height_m, DEFAULT_GNB_HEIGHT_M)
        car = Motion((0.0, 0.0, 1.5), (10.0, 0.0, 0.0), (-1.0, 1.0), "car")
        self.assertAlmostEqual(car.position_at(0.15)[0], 0.5)
        self.assertAlmostEqual(car.velocity_at(0.15)[0], -10.0)

    def test_band3_fdd_frequencies_are_direction_specific(self) -> None:
        args = parse_args([])
        self.assertEqual(args.downlink_frequency_hz, DEFAULT_DOWNLINK_FREQUENCY_HZ)
        self.assertEqual(args.uplink_frequency_hz, DEFAULT_UPLINK_FREQUENCY_HZ)
        self.assertNotEqual(args.downlink_frequency_hz, args.uplink_frequency_hz)

    def test_common_carrier_override_preserves_tdd_compatibility(self) -> None:
        args = parse_args(["--carrier-frequency-hz", "3500000000"])
        self.assertEqual(args.downlink_frequency_hz, 3_500_000_000.0)
        self.assertEqual(args.uplink_frequency_hz, 3_500_000_000.0)

    def test_environment_record_exposes_effective_solver_and_motion(self) -> None:
        args = parse_args(["--max-depth", "4", "--update-hz", "5"])
        environment = scenario_environment(args)
        self.assertEqual(environment["scene"], "simple_street_canyon")
        self.assertEqual(environment["solver"]["name"], "PathSolver")
        self.assertEqual(environment["solver"]["max_depth"], 4)
        self.assertTrue(environment["solver"]["propagation"]["los"])
        self.assertFalse(
            environment["solver"]["propagation"]["diffuse_reflection"]
        )
        self.assertEqual(environment["update_rate_hz"], 5.0)
        self.assertEqual(environment["nodes"]["gnb0"]["velocity_mps"], (0.0, 0.0, 0.0))
        self.assertEqual(environment["nodes"]["ue0"]["mobility"], "car")
        self.assertEqual(environment["nodes"]["ue1"]["mobility"], "pedestrian")
        self.assertEqual(environment["link_count"], 10)
        self.assertEqual(environment["nodes"]["gnb0"]["start_m"][2], 60.0)

    def test_floor_is_split_into_ui_visible_road_and_ground(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            output = root / "generated"
            source.mkdir()
            output.mkdir()
            write_rectangle_ply(
                source / "floor.ply",
                x_min=-10.0,
                x_max=10.0,
                y_min=-6.0,
                y_max=6.0,
                z=0.0,
            )
            write_rectangle_ply(
                source / "building_1.ply",
                x_min=-8.0,
                x_max=-5.0,
                y_min=2.0,
                y_max=5.0,
                z=2.0,
            )
            (source / "scene.xml").write_text(
                """<scene version="2.1.0">
<bsdf type="itu-radio-material" id="concrete"><string name="type" value="concrete"/></bsdf>
<shape type="ply" id="mesh-building_1"><string name="filename" value="building_1.ply"/><ref id="concrete" name="bsdf"/></shape>
<shape type="ply" id="mesh-floor"><string name="filename" value="floor.ply"/><ref id="concrete" name="bsdf"/></shape>
</scene>""",
                encoding="utf-8",
            )
            generated = prepare_scene_with_simple_road(
                source / "scene.xml", output, 4.0
            )
            geometry = scene_geometry(generated)
            objects = {item["id"]: item for item in geometry["objects"]}
            self.assertNotIn("floor", objects)
            self.assertEqual(objects["road"]["kind"], "road")
            self.assertEqual(
                objects["road"]["footprint_xy_m"],
                [[-10.0, -2.0], [10.0, -2.0], [10.0, 2.0], [-10.0, 2.0]],
            )
            self.assertIn("ground-south", objects)
            self.assertIn("ground-north", objects)
            self.assertEqual(objects["building_1"]["kind"], "building")

    def test_physical_path_conversion(self) -> None:
        coefficient = 0.1 * cmath.exp(1j * 0.75)
        taps = rays_to_taps(
            [Ray(delay_seconds=2.5 / 20_000_000.0, coefficient=coefficient)],
            sample_rate_hz=20_000_000.0,
            gain_offset_db=3.0,
        )
        self.assertEqual(len(taps), 1)
        self.assertAlmostEqual(taps[0].delay_samples, 2.5)
        self.assertAlmostEqual(taps[0].gain_db, -17.0)
        self.assertAlmostEqual(taps[0].phase_rad, 0.75)

    def test_same_delay_bin_is_merged_coherently(self) -> None:
        sample_rate = 16.0
        rays = [
            Ray(1.01 / sample_rate, 1.0 + 0.0j),
            Ray(1.02 / sample_rate, 0.0 + 1.0j),
        ]
        taps = rays_to_taps(rays, sample_rate_hz=sample_rate)
        self.assertEqual(len(taps), 1)
        self.assertAlmostEqual(taps[0].delay_samples, 1.0)
        self.assertAlmostEqual(taps[0].gain_db, 20.0 * math.log10(math.sqrt(2.0)))
        self.assertAlmostEqual(taps[0].phase_rad, math.pi / 4.0)

    def test_strongest_bins_are_capped_and_delay_sorted(self) -> None:
        rays = [Ray(float(index), complex(index + 1, 0.0)) for index in range(5)]
        taps = rays_to_taps(
            rays,
            sample_rate_hz=1.0,
            max_taps=2,
            max_gain_db=20.0,
        )
        self.assertEqual([tap.delay_samples for tap in taps], [3.0, 4.0])

    def test_invalid_or_missing_paths_become_quiet_outage(self) -> None:
        taps = rays_to_taps(
            [Ray(float("nan"), 1.0 + 0.0j), Ray(0.0, 0.0j)],
            sample_rate_hz=23_040_000.0,
        )
        self.assertEqual(taps, [Tap(0.0, -100.0, 0.0)])

    def test_profile_contract_and_canonical_link_id(self) -> None:
        link_id = control_link_id("gnb0", "ue1", "sionna_rt")
        self.assertEqual(link_id, "gnb0>ue1:sionna_rt")
        message = make_profile_swap(
            link_id,
            [Tap(1.5, -12.0, 0.25)],
            batch_id="sionna-7",
        )
        self.assertEqual(message["type"], "profile_swap")
        self.assertEqual(message["batch_id"], "sionna-7")
        self.assertEqual(message["taps"][0]["delay_samples"], 1.5)
        self.assertFalse(message["fading"]["enabled"])
        self.assertNotIn("scene_geometry", message)

    def test_matrix_profile_contract_covers_every_lane_in_row_major_order(self) -> None:
        profile = MatrixProfile(
            nt=2,
            nr=2,
            lanes=(
                LaneProfile(1, 1, (Tap(0.0, -4.0, 0.4),)),
                LaneProfile(0, 1, (Tap(0.0, -2.0, 0.2),)),
                LaneProfile(1, 0, (Tap(0.0, -3.0, 0.3),)),
                LaneProfile(0, 0, (Tap(0.0, -1.0, 0.1),)),
            ),
        )
        message = make_matrix_profile_swap(
            "gnb0>ue0:desired", profile, batch_id="sionna-9"
        )
        self.assertEqual(message["type"], "matrix_profile_swap")
        self.assertEqual((message["nr"], message["nt"]), (2, 2))
        self.assertEqual(
            [(lane["rx_port"], lane["tx_port"]) for lane in message["lanes"]],
            [(0, 0), (0, 1), (1, 0), (1, 1)],
        )

    def test_scenario_config_drives_nodes_links_and_array_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "scenario.json"
            path.write_text(
                """{
  "name":"dynamic-miso",
  "nodes":{
    "gnb":{"start_m":[0,0,10],"tx_array":{"rows":2,"cols":2}},
    "ue":{"start_m":[10,0,1.5],"rx_array":{"rows":1,"cols":2}}
  },
  "links":[{"from":"gnb","to":"ue","direction":"downlink","model":"h"}]
}""",
                encoding="utf-8",
            )
            definition = load_scenario_config(path)
            self.assertEqual(definition.nodes["gnb"].tx_array.antenna_count, 4)
            self.assertEqual(definition.nodes["ue"].rx_array.antenna_count, 2)
            args = parse_args(["--scenario-config", str(path)])
            self.assertEqual(effective_scenario(args).name, "dynamic-miso")
            environment = scenario_environment(args)
            self.assertEqual(environment["matrix_lane_count"], 8)
            self.assertEqual(environment["nodes"]["gnb"]["tx_antennas"], 4)

    def test_channel_status_exposes_the_exact_ui_tap_values(self) -> None:
        taps = [Tap(2.5, -12.0, math.pi / 2.0)]
        status = channel_status(
            "gnb0>ue0:sionna_rt",
            [Ray(2.5 / 20_000_000.0, 0.25 + 0.0j, -37.5)],
            taps,
            sample_rate_hz=20_000_000.0,
        )
        self.assertEqual(status["tap_count"], 1)
        self.assertEqual(status["taps"][0]["delay_samples"], 2.5)
        self.assertAlmostEqual(status["taps"][0]["delay_ns"], 125.0)
        self.assertEqual(status["taps"][0]["gain_db"], -12.0)
        self.assertAlmostEqual(status["taps"][0]["phase_deg"], 90.0)
        self.assertEqual(status["delay_doppler_point_count"], 1)
        point = status["delay_doppler_points"][0]
        self.assertAlmostEqual(point["delay_samples"], 2.5)
        self.assertAlmostEqual(point["delay_ns"], 125.0)
        self.assertAlmostEqual(point["doppler_hz"], -37.5)
        self.assertAlmostEqual(point["power_db"], 20.0 * math.log10(0.25))

    def test_miso_profiles_broadcast_sionna_path_doppler_to_each_lane(self) -> None:
        class FakeTensor:
            def __init__(self, ndim: int, values: object) -> None:
                self.ndim = ndim
                self.values = values

            def __getitem__(self, key: tuple[object, ...]) -> object:
                if self.ndim == 6:
                    return self.values[key[3]]  # type: ignore[index]
                return self.values

        class FakeNumpy:
            @staticmethod
            def asarray(value: object) -> object:
                return value

        class FakePaths:
            doppler = FakeTensor(3, [-25.0, 40.0])
            valid = FakeTensor(3, [True, True])

            @staticmethod
            def cir(**_kwargs: object) -> tuple[FakeTensor, FakeTensor]:
                return (
                    FakeTensor(
                        6,
                        {
                            0: [0.25 + 0.0j, 0.125 + 0.0j],
                            1: [0.5 + 0.0j, 0.0625 + 0.0j],
                        },
                    ),
                    FakeTensor(3, [1.0e-7, 2.0e-7]),
                )

        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "miso.json"
            path.write_text(
                """{
  "name":"miso",
  "nodes":{
    "gnb":{"start_m":[0,0,10],"tx_array":{"rows":1,"cols":2}},
    "ue":{"start_m":[10,0,1.5]}
  },
  "links":[{"from":"gnb","to":"ue","direction":"downlink","model":"h"}]
}""",
                encoding="utf-8",
            )
            args = parse_args(["--scenario-config", str(path)])
            scenario = object.__new__(SionnaScenario)
            scenario.args = args
            scenario.np = FakeNumpy()
            scenario.definition = effective_scenario(args)
            scenario.tx_indices = {"gnb": 0}
            scenario.rx_indices = {"ue": 0}
            link = scenario.definition.links[0]
            profiles, statuses = scenario.profiles(
                FakePaths(),
                [link],
                direction="downlink",
                carrier_frequency_hz=args.downlink_frequency_hz,
            )

        profile = profiles["gnb>ue:h"]
        self.assertEqual((profile.nr, profile.nt), (1, 2))
        self.assertEqual(len(statuses[0]["lanes"]), 2)
        for lane in statuses[0]["lanes"]:
            self.assertEqual(
                [point["doppler_hz"] for point in lane["delay_doppler_points"]],
                [-25.0, 40.0],
            )


if __name__ == "__main__":
    unittest.main()
