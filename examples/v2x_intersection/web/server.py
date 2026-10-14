#!/usr/bin/env python3
"""V2X dashboard server.

Serves `web/index.html` and adds two read-only APIs to the gate's shared web
UI (`scripts/web_ui/server.py`), whose endpoints it proxies unchanged:

  /api/status, /api/realtime, /api/scene_mesh, /healthz   -> --upstream
  /api/road    the SUMO network as drawn: lane polygons, junction outline,
               stop lines, building blocks, radio sites (built once with
               sumo_world.build_network from layout.py)
  /api/vehicles  the positions feed's newest frame only (the 3D scene, 10 Hz)
  /api/v2x     live vehicles from the positions feed (`drive.py` PUB), the
               running episode's last rows (controller state, speed, accel,
               V2X reception) and recent episode results

Run it with the external interpreter (`~/venvs/v2x/bin/v2x-python`):

    web/server.py --port 8080 --upstream http://127.0.0.1:8081 \
        --positions-endpoint ipc://$OCUDU_NATIVE_ROOT/run/v2x/positions.sock \
        --logs-root $OCUDU_NATIVE_ROOT/results/logs/ocudu-multi-ue

It only reads files and the positions feed; it never signals a process.
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
PKG = HERE.parent
REPO = PKG.parents[1]
VENDOR = REPO / "scripts" / "web_ui" / "vendor"
sys.path.insert(0, str(PKG))

import layout  # noqa: E402

PROXIED = ("/api/status", "/api/realtime", "/api/scene_mesh", "/healthz")


def road_geometry() -> dict:
    """SUMO's own lane and junction shapes for the layout's network."""

    import sumo_world

    with tempfile.TemporaryDirectory(prefix="v2x-net-") as tmp:
        net = sumo_world.sumolib.net.readNet(str(sumo_world.build_network(pathlib.Path(tmp))),
                                             withInternal=True)
        lanes = []
        for edge in net.getEdges(withInternal=True):
            for lane in edge.getLanes():
                lanes.append({"id": lane.getID(), "internal": edge.getFunction() == "internal",
                              "width_m": lane.getWidth(), "shape": [list(p) for p in lane.getShape()]})
        junctions = [{"id": node.getID(), "shape": [list(p) for p in node.getShape()]}
                     for node in net.getNodes() if node.getShape()]
        version = sumo_world.sumo_version()
    cp = layout.CONFLICT_POINT_M
    gap = layout.STOP_LINE_GAP_M
    return {
        "sumo_version": version,
        "lanes": lanes,
        "junctions": junctions,
        "lane_width_m": layout.LANE_WIDTH_M,
        "setback_m": layout.SETBACK_M,
        "conflict_point_m": list(cp),
        # Stop lines across each vehicle's own lane (A northbound, B westbound).
        "stop_lines": [
            {"vehicle": "A", "from": [cp[0] - layout.LANE_WIDTH_M / 2, cp[1] - gap],
             "to": [cp[0] + layout.LANE_WIDTH_M / 2, cp[1] - gap]},
            {"vehicle": "B", "from": [cp[0] + gap, cp[1] - layout.LANE_WIDTH_M / 2],
             "to": [cp[0] + gap, cp[1] + layout.LANE_WIDTH_M / 2]},
        ],
        "blocks": [{"id": b.name, "x": [b.x_min, b.x_max], "y": [b.y_min, b.y_max], "height_m": b.height_m}
                   for b in layout.BLOCKS],
        "ground_half_extent_m": layout.GROUND_HALF_EXTENT_M,
        "vehicle": {"length_m": layout.VEHICLE_LENGTH_M, "width_m": layout.VEHICLE_WIDTH_M,
                    "antenna_height_m": layout.ANTENNA_HEIGHT_M},
        "radio_sites": {name: {"gnb_m": list(s.gnb_m), "parked_ue_m": list(s.parked_ue_m)}
                        for name, s in layout.RADIO_SITES.items()},
    }


class LiveVehicles:
    """Latest positions-feed frame plus a short history, from a SUB thread."""

    def __init__(self, endpoint: str | None, history_s: float = 30.0) -> None:
        self.endpoint = endpoint
        self.latest: dict | None = None
        self.received_monotonic = 0.0
        self.history: deque = deque(maxlen=int(history_s * 20) + 20)
        self.lock = threading.Lock()
        if endpoint:
            threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        import zmq

        sub = zmq.Context.instance().socket(zmq.SUB)
        sub.setsockopt(zmq.LINGER, 0)
        sub.setsockopt_string(zmq.SUBSCRIBE, "")
        sub.connect(self.endpoint)
        while True:
            frame = json.loads(sub.recv())
            vehicles = frame.get("vehicles") or {}
            with self.lock:
                self.latest = frame
                self.received_monotonic = time.monotonic()
                self.history.append({
                    "t_unix_ms": frame.get("t_unix_ms"), "sim_time_s": frame.get("sim_time_s"),
                    **{f"{vid}_speed": v.get("speed_mps") for vid, v in vehicles.items()},
                })

    def snapshot(self) -> dict:
        with self.lock:
            if self.latest is None:
                return {"endpoint": self.endpoint, "connected": False}
            age_ms = (time.monotonic() - self.received_monotonic) * 1000.0
            return {"endpoint": self.endpoint, "connected": True, "age_ms": round(age_ms, 1),
                    "stale": age_ms > 1000.0, "frame": self.latest}


def tail_csv(path: pathlib.Path, rows: int) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(deque(reader, maxlen=rows))


class Episodes:
    """The newest run directory with V2X episodes, followed as it grows."""

    def __init__(self, logs_root: pathlib.Path | None, run_dir: pathlib.Path | None) -> None:
        self.logs_root = logs_root
        self.run_dir = run_dir

    def v2x_dir(self) -> pathlib.Path | None:
        if self.run_dir is not None:
            run = self.run_dir
        elif self.logs_root is not None and self.logs_root.is_dir():
            runs = sorted((p for p in self.logs_root.iterdir() if (p / "v2x").is_dir()), reverse=True)
            if not runs:
                return None
            run = runs[0]
        else:
            return None
        return run / "v2x" if (run / "v2x").is_dir() else run

    def snapshot(self) -> dict:
        v2x = self.v2x_dir()
        if v2x is None or not (v2x / "episodes").is_dir():
            return {"run": None}
        dirs = sorted(p for p in (v2x / "episodes").iterdir() if p.is_dir())
        results = []
        for d in dirs[-12:]:
            path = d / "result.json"
            if path.exists():
                try:
                    r = json.loads(path.read_text())
                except ValueError:
                    continue
                results.append({"episode": d.name, "mode": r.get("label"), "ended": r.get("ended"),
                                "collision": r.get("collision"), "min_centre_gap_m": r.get("min_centre_gap_m"),
                                "first_risk_s": r.get("first_risk_sim_time_s"),
                                "first_brake_s": r.get("first_brake_sim_time_s"),
                                "v2x_received": (r.get("v2x") or {}).get("messages_received"),
                                "one_way_ms_p50": ((r.get("v2x") or {}).get("one_way_ms") or {}).get("p50"),
                                "rtf": (r.get("timing") or {}).get("rtf")})
        current = dirs[-1] if dirs else None
        running = current is not None and not (current / "result.json").exists()
        rows = tail_csv(current / "vehicles.csv", 1200) if current else []
        a_rows = [r for r in rows if r.get("vehicle_id") == "A"]
        b_rows = [r for r in rows if r.get("vehicle_id") == "B"]
        rx = tail_csv(current / "v2x_rx.csv", 400) if current else []
        series = [{"t": float(a["simulation_time"]), "a_speed": float(a["speed_mps"]),
                   "b_speed": float(b["speed_mps"]), "target": float(a["target_speed_mps"] or 0.0),
                   "accel": float(a["accel_applied_mps2"] or 0.0), "state": a["controller_state"],
                   "info": a["info"]}
                  for a, b in zip(a_rows[::2], b_rows[::2])]
        return {
            "run": str(v2x.parent if v2x.name == "v2x" else v2x),
            "episode": None if current is None else current.name,
            "running": running,
            "now": a_rows[-1] if a_rows else None,
            "b_now": b_rows[-1] if b_rows else None,
            "series": series,
            "rx": [{"t": float(r["simulation_time_at_receive"]), "one_way_ms": float(r["one_way_ms"]),
                    "seq": int(r["sequence"])} for r in rx],
            "results": results,
        }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--upstream", default="http://127.0.0.1:8081", help="the gate's shared web UI")
    ap.add_argument("--positions-endpoint", help="drive.py's ZMQ PUB endpoint")
    ap.add_argument("--logs-root", type=pathlib.Path, help="follow the newest run with a v2x/ directory")
    ap.add_argument("--run-dir", type=pathlib.Path, help="a fixed run (gate log dir or run_local.py --out)")
    ap.add_argument("--index", type=pathlib.Path, default=HERE / "index.html")
    args = ap.parse_args(argv)

    road = json.dumps(road_geometry()).encode()
    live = LiveVehicles(args.positions_endpoint)
    episodes = Episodes(args.logs_root, args.run_dir)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused) -> None:
            pass

        def send(self, status, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, value) -> None:
            self.send(HTTPStatus.OK, json.dumps(value).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            url = urllib.parse.urlparse(self.path)
            try:
                if url.path in ("/", "/index.html"):
                    return self.send(HTTPStatus.OK, args.index.read_bytes(), "text/html; charset=utf-8")
                if url.path.startswith("/vendor/"):
                    path = (VENDOR / url.path[len("/vendor/"):]).resolve()
                    if path.parent != VENDOR.resolve() or path.suffix != ".js" or not path.is_file():
                        return self.send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                    return self.send(HTTPStatus.OK, path.read_bytes(), "text/javascript; charset=utf-8")
                if url.path == "/api/road":
                    return self.send(HTTPStatus.OK, road, "application/json")
                if url.path == "/api/vehicles":
                    return self.send_json(live.snapshot())
                if url.path == "/api/v2x":
                    return self.send_json({"live": live.snapshot(), **episodes.snapshot()})
                if url.path in PROXIED:
                    try:
                        with urllib.request.urlopen(args.upstream + self.path, timeout=3) as response:
                            return self.send(response.status, response.read(),
                                             response.headers.get("Content-Type", "application/json"))
                    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                        body = json.dumps({"error": f"upstream {args.upstream}: {exc}"}).encode()
                        return self.send(HTTPStatus.BAD_GATEWAY, body, "application/json")
                return self.send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            except (BrokenPipeError, ConnectionResetError):
                return None

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"V2X dashboard: http://{args.bind}:{args.port} (upstream {args.upstream})", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
