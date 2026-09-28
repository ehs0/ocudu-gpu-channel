"""Receive robot poses published by Isaac Sim.

Isaac steps physics at a fixed rate that has nothing to do with how long a
ray-tracing solve takes, so the two must not be locked together: whichever
is slower would drag the other down, and the IQ path downstream cannot
tolerate a stall. The socket is therefore a conflating SUB -- Isaac publishes
freely, the bridge reads whatever the newest frame happens to be, and a frame
that is overtaken before it is read is simply dropped. Dropping it is correct:
a stale pose is worth nothing once a newer one exists.

Frame format, one JSON object per publish:

    {"ue0": {"position_m": [x, y, z], "velocity_mps": [vx, vy, vz]}, ...}

Coordinates are Sionna scene metres, which the room generator arranges to be
identical to Isaac's stage coordinates (Z up, metres, origin at the centre of
the room floor). Nothing converts between them, by design.
"""

from __future__ import annotations

import json
from typing import Any

import zmq


class IsaacPositionSource:
    """Latest-frame-wins pose feed from Isaac Sim."""

    def __init__(self, endpoint: str, *, context: zmq.Context | None = None) -> None:
        context = context or zmq.Context.instance()
        self._socket = context.socket(zmq.SUB)
        # CONFLATE keeps a single message in the queue. Without it a bridge
        # that solves slower than Isaac publishes would work through an
        # ever-lengthening backlog of poses the robot left behind minutes ago.
        self._socket.setsockopt(zmq.CONFLATE, 1)
        self._socket.setsockopt(zmq.RCVHWM, 1)
        self._socket.setsockopt_string(zmq.SUBSCRIBE, "")
        self._socket.connect(endpoint)
        self.endpoint = endpoint
        self.frames_received = 0
        self._latest: dict[str, Any] = {}

    def poll(self) -> dict[str, Any]:
        """Return the newest frame, or the previous one if none has arrived.

        Never blocks. Before the first frame this returns an empty dict, which
        callers read as "no external pose yet" and fall back to the configured
        starting position.
        """

        while True:
            try:
                payload = self._socket.recv_string(zmq.NOBLOCK)
            except zmq.Again:
                break
            try:
                frame = json.loads(payload)
            except json.JSONDecodeError:
                # A malformed publish is not worth killing a running
                # simulation for; hold the last good pose and carry on.
                continue
            if isinstance(frame, dict):
                self._latest = frame
                self.frames_received += 1
        return self._latest

    def close(self) -> None:
        self._socket.close(linger=0)
