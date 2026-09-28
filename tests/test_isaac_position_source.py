#!/usr/bin/env python3
"""Tests for the Isaac Sim pose feed and the room scene it shares with it."""

from __future__ import annotations

import json
import pathlib
import sys
import time
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "sionna_rt"))

import zmq  # noqa: E402

from build_room_scene import box, room_boxes  # noqa: E402
from isaac_source import IsaacPositionSource  # noqa: E402


class RoomGeometryTest(unittest.TestCase):
    """The room is the one thing Isaac and Sionna must agree on exactly."""

    @staticmethod
    def _args(**overrides):
        defaults = {
            "room_span_m": 10.0,
            "room_height_m": 3.0,
            "wall_thickness_m": 0.2,
            "table_size_m": [2.0, 1.2, 0.75],
            "table_center_m": [0.0, 0.0],
        }
        defaults.update(overrides)
        return type("Args", (), defaults)

    def test_box_faces_are_triangles(self) -> None:
        # Mitsuba's PLY loader refuses quads, and the failure only surfaces
        # when a scene is loaded, so it is pinned here instead.
        _, faces = box([0.0, 0.0, 0.0], [1.0, 1.0, 1.0])
        self.assertEqual(len(faces), 12)
        self.assertTrue(all(len(face) == 3 for face in faces))

    def test_box_normals_point_outward(self) -> None:
        vertices, faces = box([0.0, 0.0, 0.0], [2.0, 4.0, 6.0])
        for face in faces:
            a, b, c = (vertices[index] for index in face)
            edge1 = [b[i] - a[i] for i in range(3)]
            edge2 = [c[i] - b[i] for i in range(3)]
            normal = [
                edge1[1] * edge2[2] - edge1[2] * edge2[1],
                edge1[2] * edge2[0] - edge1[0] * edge2[2],
                edge1[0] * edge2[1] - edge1[1] * edge2[0],
            ]
            centroid = [sum(v[i] for v in (a, b, c)) / 3.0 for i in range(3)]
            # The box is centred on the origin, so any outward normal has a
            # positive projection onto the vector from the centre.
            self.assertGreater(sum(normal[i] * centroid[i] for i in range(3)), 0.0)

    def test_walls_leave_the_interior_clear(self) -> None:
        boxes = room_boxes(self._args())
        by_id = {item["id"]: item for item in boxes}
        span = 10.0
        for wall, axis in (("building_wall_east", 0), ("building_wall_north", 1)):
            near_face = by_id[wall]["center"][axis] - by_id[wall]["size"][axis] / 2.0
            self.assertAlmostEqual(near_face, span / 2.0, places=6)

    def test_table_sits_on_the_floor(self) -> None:
        table = next(i for i in room_boxes(self._args()) if i["id"] == "wood_table")
        self.assertAlmostEqual(table["center"][2] - table["size"][2] / 2.0, 0.0, places=6)
        self.assertAlmostEqual(table["center"][2] + table["size"][2] / 2.0, 0.75, places=6)

    def test_object_ids_classify_for_the_viewer(self) -> None:
        from run_bridge import surface_kind

        kinds = {item["id"]: surface_kind(item["id"]) for item in room_boxes(self._args())}
        self.assertEqual(kinds["wood_table"], "wood")
        self.assertEqual(kinds["building_wall_north"], "building")


class IsaacPositionSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.context = zmq.Context()
        self.publisher = self.context.socket(zmq.PUB)
        self.port = self.publisher.bind_to_random_port("tcp://127.0.0.1")
        self.source = IsaacPositionSource(
            f"tcp://127.0.0.1:{self.port}", context=self.context
        )
        self.addCleanup(self.context.term)
        self.addCleanup(self.publisher.close)
        self.addCleanup(self.source.close)

    def _publish_until_received(self, payload: str, attempts: int = 200) -> dict:
        # PUB/SUB drops anything sent before the subscription is established,
        # so the first frame is resent until it lands. The pause matters: a
        # tight loop never yields long enough for the handshake to finish.
        for _ in range(attempts):
            self.publisher.send_string(payload)
            frame = self.source.poll()
            if frame:
                return frame
            time.sleep(0.005)
        self.fail("no frame arrived")

    def test_poll_is_empty_before_any_frame(self) -> None:
        self.assertEqual(self.source.poll(), {})

    def test_latest_frame_wins(self) -> None:
        self._publish_until_received(json.dumps(
            {"ue0": {"position_m": [1.0, 2.0, 0.4]}}
        ))
        newest = json.dumps({"ue0": {"position_m": [9.0, 9.0, 0.4]}})
        for _ in range(20):
            self.publisher.send_string(newest)
        frame = {}
        for _ in range(200):
            frame = self.source.poll()
            if frame.get("ue0", {}).get("position_m") == [9.0, 9.0, 0.4]:
                break
            time.sleep(0.005)
        # A backlog must never build up: the pose the robot has now is the
        # only one worth tracing.
        self.assertEqual(frame["ue0"]["position_m"], [9.0, 9.0, 0.4])

    def test_malformed_payload_keeps_the_last_good_pose(self) -> None:
        good = self._publish_until_received(json.dumps(
            {"ue0": {"position_m": [1.0, 2.0, 0.4]}}
        ))
        for _ in range(20):
            self.publisher.send_string("{not json")
        for _ in range(50):
            self.assertEqual(self.source.poll(), good)


if __name__ == "__main__":
    unittest.main()
