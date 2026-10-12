"""Wire formats of the V2X demo (stdlib only; `ue_client.py` runs on any python3).

Three planes:

1. **V2X message** (roadside server -> vehicle A, UDP over the 5G user plane):
   one JSON object per datagram, `message_type` "vehicle_state".
2. **In-vehicle relay** (UE client -> vehicle A controller, unix datagram):
   an 8-byte little-endian receive timestamp (unix ns, taken when the UE's
   socket returned the datagram) followed by the V2X datagram unchanged.
3. **Positions feed** (simulator -> Sionna bridge and roadside server, ZMQ
   PUB): the bridge's `{"event": "positions", "frame": "arena", "nodes": ...}`
   message (`scripts/sionna_rt/run_bridge.py`, `ExternalPositionSource`),
   plus a `vehicles` object the bridge ignores and the roadside server reads.
   `nodes` carries only radio nodes (vehicle A's UE); `frame` is "arena" because
   that is the only frame the bridge accepts, and here the arena frame is the
   scene frame (`layout.py`), so the bridge runs with a zero offset.
"""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass
from typing import Any

MESSAGE_TYPE = "vehicle_state"
MAX_DATAGRAM_BYTES = 1200       # well inside one PDCP SDU / IPv4 MTU
RELAY_HEADER = struct.Struct("<Q")


@dataclass(frozen=True)
class VehicleStateMessage:
    vehicle_id: str
    sequence: int
    simulation_time: float
    send_wall_time_ns: int
    position: tuple[float, float, float]
    velocity: tuple[float, float, float]

    @property
    def speed_mps(self) -> float:
        return math.hypot(self.velocity[0], self.velocity[1])


def encode(message: VehicleStateMessage) -> bytes:
    body = {
        "message_type": MESSAGE_TYPE,
        "vehicle_id": message.vehicle_id,
        "sequence": message.sequence,
        "simulation_time": round(message.simulation_time, 4),
        "send_wall_time_ns": message.send_wall_time_ns,
        "position": dict(zip("xyz", (round(v, 3) for v in message.position))),
        "velocity": dict(zip(("vx", "vy", "vz"), (round(v, 3) for v in message.velocity))),
    }
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_DATAGRAM_BYTES:
        raise ValueError(f"V2X message is {len(data)} bytes, limit {MAX_DATAGRAM_BYTES}")
    return data


def _finite(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{where} must be a finite number")
    return float(value)


def decode(data: bytes) -> VehicleStateMessage:
    """Parse and validate one V2X datagram; raises ValueError on anything malformed."""

    try:
        body = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"not JSON: {exc}") from exc
    if not isinstance(body, dict) or body.get("message_type") != MESSAGE_TYPE:
        raise ValueError("not a vehicle_state message")
    vehicle_id = body.get("vehicle_id")
    if not isinstance(vehicle_id, str) or not vehicle_id:
        raise ValueError("vehicle_id must be a non-empty string")
    sequence = body.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("sequence must be a non-negative integer")
    send_ns = body.get("send_wall_time_ns")
    if isinstance(send_ns, bool) or not isinstance(send_ns, int) or send_ns <= 0:
        raise ValueError("send_wall_time_ns must be a positive integer")
    position = body.get("position")
    velocity = body.get("velocity")
    if not isinstance(position, dict) or not isinstance(velocity, dict):
        raise ValueError("position and velocity must be objects")
    return VehicleStateMessage(
        vehicle_id=vehicle_id,
        sequence=sequence,
        simulation_time=_finite(body.get("simulation_time"), "simulation_time"),
        send_wall_time_ns=send_ns,
        position=tuple(_finite(position.get(k), f"position.{k}") for k in "xyz"),  # type: ignore[arg-type]
        velocity=tuple(_finite(velocity.get(k), f"velocity.{k}") for k in ("vx", "vy", "vz")),  # type: ignore[arg-type]
    )


def pack_relay(receive_wall_time_ns: int, datagram: bytes) -> bytes:
    return RELAY_HEADER.pack(receive_wall_time_ns) + datagram


def unpack_relay(data: bytes) -> tuple[int, bytes]:
    if len(data) < RELAY_HEADER.size:
        raise ValueError("relay frame shorter than its header")
    (receive_ns,) = RELAY_HEADER.unpack_from(data)
    return receive_ns, data[RELAY_HEADER.size:]


def positions_message(
    *,
    t_unix_ms: int,
    simulation_time: float,
    radio_nodes: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]],
    vehicles: dict[str, dict[str, Any]],
) -> str:
    """The positions-feed frame: `nodes` for the bridge, `vehicles` for the roadside server."""

    return json.dumps(
        {
            "event": "positions",
            "t_unix_ms": t_unix_ms,
            "frame": "arena",
            "sim_time_s": round(simulation_time, 4),
            "nodes": {
                node_id: {
                    "position_m": [round(v, 3) for v in position],
                    "velocity_mps": [round(v, 3) for v in velocity],
                }
                for node_id, (position, velocity) in radio_nodes.items()
            },
            "vehicles": vehicles,
        },
        separators=(",", ":"),
    )
