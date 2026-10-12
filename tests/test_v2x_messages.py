"""V2X demo wire formats: the V2X message, the in-vehicle relay frame, and the
positions feed as the Sionna bridge's own parser reads it."""

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "v2x_intersection"))
sys.path.insert(0, str(ROOT / "scripts" / "sionna_rt"))

import messages  # noqa: E402
from run_bridge import ExternalPositionSource  # noqa: E402


def sample(**overrides):
    fields = dict(vehicle_id="B", sequence=157, simulation_time=12.5, send_wall_time_ns=1_790_000_000_123_456_789,
                  position=(32.5, 1.75, 0.0), velocity=(-13.89, 0.0, 0.0))
    fields.update(overrides)
    return messages.VehicleStateMessage(**fields)


def test_round_trip_matches_spec_schema():
    data = messages.encode(sample())
    body = json.loads(data)
    assert body["message_type"] == "vehicle_state"
    assert set(body) == {"message_type", "vehicle_id", "sequence", "simulation_time",
                         "send_wall_time_ns", "position", "velocity"}
    assert set(body["position"]) == {"x", "y", "z"}
    assert set(body["velocity"]) == {"vx", "vy", "vz"}
    got = messages.decode(data)
    assert got.sequence == 157
    assert got.send_wall_time_ns == 1_790_000_000_123_456_789
    assert got.position == pytest.approx((32.5, 1.75, 0.0))
    assert got.speed_mps == pytest.approx(13.89)
    assert len(data) < messages.MAX_DATAGRAM_BYTES


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(message_type="other"),
    lambda b: b.update(sequence=-1),
    lambda b: b.update(sequence=True),
    lambda b: b.update(send_wall_time_ns=0),
    lambda b: b["position"].update(x=float("nan")),
    lambda b: b["velocity"].pop("vy"),
    lambda b: b.update(vehicle_id=""),
])
def test_decode_rejects_malformed(mutate):
    body = json.loads(messages.encode(sample()))
    mutate(body)
    with pytest.raises(ValueError):
        messages.decode(json.dumps(body).encode())


def test_decode_rejects_non_json():
    with pytest.raises(ValueError):
        messages.decode(b"\xff\x00")


def test_relay_frame_round_trip():
    data = messages.encode(sample())
    frame = messages.pack_relay(1_790_000_000_200_000_000, data)
    receive_ns, payload = messages.unpack_relay(frame)
    assert receive_ns == 1_790_000_000_200_000_000
    assert payload == data
    with pytest.raises(ValueError):
        messages.unpack_relay(b"short")


class OneFrame:
    def __init__(self, frame: str):
        self.frames = [frame.encode()]

    def recv_noblock(self):
        return self.frames.pop(0) if self.frames else None


def test_positions_feed_is_accepted_by_the_bridge_parser():
    frame = messages.positions_message(
        t_unix_ms=1_790_000_000_000, simulation_time=3.25,
        radio_nodes={"ue0": ((1.75, -60.0, 1.5), (0.0, 13.89, 0.0))},
        vehicles={"A": {"position_m": [1.75, -60.0, 0.0]}, "B": {"position_m": [60.0, 1.75, 0.0]}},
    )
    warnings = []
    source = ExternalPositionSource("inproc://test", known_nodes=("gnb0", "ue0", "ue1"),
                                    transport=OneFrame(frame), clock=lambda: 10.0, warn=warnings.append)
    got = source.poll()
    assert got is not None, source.last_error
    assert got.nodes["ue0"][0] == pytest.approx((1.75, -60.0, 1.5))
    assert got.nodes["ue0"][1] == pytest.approx((0.0, 13.89, 0.0))
    assert set(got.nodes) == {"ue0"}
    assert warnings == []           # vehicles are not radio nodes and are not reported as unknown
