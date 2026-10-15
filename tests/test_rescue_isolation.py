"""Information separation: robots and the mission server cannot see ground truth or bypass the UE link.

Static checks on the shipped sources (what each process imports and opens),
plus the sensor frame the world sends a robot. The episode test checks the
dynamic side (a blacked-out robot's events do not reach the server).
"""

import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "multi_robot_rescue"
sys.path.insert(0, str(EXAMPLE))

import layout  # noqa: E402


def names_used(path):
    tree = ast.parse(path.read_text())
    imports, attributes, strings = set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
        elif isinstance(node, ast.Attribute):
            attributes.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.add(node.value)
    return imports, attributes, strings


TRUTH_ATTRIBUTES = {"SURVIVOR_PRESETS", "survivor_positions", "free_cells", "truth_free_mask", "known_fraction"}


def test_robot_has_no_network_socket_and_no_truth():
    imports, attributes, strings = names_used(EXAMPLE / "robot.py")
    assert "zmq" not in imports
    assert "AF_INET" not in attributes and "AF_INET6" not in attributes
    assert "AF_UNIX" in attributes
    assert not (attributes & TRUTH_ATTRIBUTES)
    assert "referee" not in strings and "survivors" not in strings


def test_mission_server_reads_only_its_socket():
    imports, attributes, strings = names_used(EXAMPLE / "mission_server.py")
    assert "zmq" not in imports                       # never subscribes to the positions feed
    assert not (imports & {"sim", "kinematic_world", "isaac_world", "sensors"})
    assert not (attributes & TRUTH_ATTRIBUTES)
    assert "referee" not in strings


def test_ue_link_is_stdlib_and_the_only_inet_user_on_the_robot_side():
    imports, attributes, _ = names_used(EXAMPLE / "ue_link.py")
    allowed = {"argparse", "csv", "errno", "json", "os", "pathlib", "select", "signal", "socket", "sys", "time",
               "messages", "offline_queue", "__future__"}
    assert imports <= allowed
    for module in ("messages.py", "offline_queue.py"):
        mod_imports, _, _ = names_used(EXAMPLE / module)
        assert mod_imports <= {"base64", "json", "math", "time", "typing", "pathlib", "sqlite3", "dataclasses",
                               "__future__"}


def test_sensor_frame_carries_no_identity_or_truth():
    from kinematic_world import KinematicWorld
    world = KinematicWorld(((3.0, 12.0), (30.0, 5.0)))
    world.pose["robot_2"] = [1.5, 12.0, 0.0]          # survivor 1 in view, 1.5 m ahead
    dets = world.detections("robot_2")
    assert len(dets) == 1
    assert set(dets[0]) == {"bearing_rad", "range_m", "confidence"}
    text = json.dumps(dets)
    assert "3.0" not in text and "30.0" not in text


def test_hidden_survivor_is_not_detected_through_a_wall():
    from kinematic_world import KinematicWorld
    hidden = layout.SURVIVOR_PRESETS["demo"][0]            # inside the steel room
    world = KinematicWorld((hidden,))
    world.pose["robot_1"] = [hidden[0], 12.0, 1.5708]      # corridor, facing the room, 10 m away
    assert world.detections("robot_1") == []
    world.pose["robot_1"] = [hidden[0], 19.5, 1.5708]      # inside, 2.5 m away
    assert len(world.detections("robot_1")) == 1
