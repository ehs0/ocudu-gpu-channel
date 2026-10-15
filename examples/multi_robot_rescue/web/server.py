#!/usr/bin/env python3
"""Rescue dashboard server (stdlib http.server; pyzmq for the positions feed).

Serves `index.html` and three JSON endpoints, all read-only:

- `/api/layout`: the building (walls, rubble, rooms, radio sites, grid) from layout.py;
- `/api/state`: the running episode — `current.json` (mode labels), the mission
  server's published state (what the central side knows), every UE link's and
  robot's status file, and the newest positions-feed frame (the referee's
  truth: robot poses and survivors, shown only in the labelled referee view);
- `/api/radio`: where the gate's shared web UI is (`--radio-ui`) for the RF,
  scheduler and GPU panels, which this page links rather than copies;
- `/frames/<view>.jpg`: the newest rendered view (`overview`, `robot_N`) that
  sim.py `--frames-dir` writes in the Isaac world; 404 when there is none.

`--run-dir` holds `current.json` (written by launch/root_exec.sh, or by
run_local.py `--web-status-dir`); `current.json` names the directory with the
status files and, for local runs, the positions endpoint.
"""

from __future__ import annotations

import argparse
import http.server
import json
import pathlib
import sys
import threading

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import layout  # noqa: E402

VIEWS = ("overview",) + layout.ROBOT_IDS


def layout_body() -> dict:
    import grid
    return {
        "building_m": {"x": list(layout.BUILDING_X_M), "y": list(layout.BUILDING_Y_M)},
        "walls": [[b.x0, b.y0, b.x1, b.y1, b.material] for b in layout.WALLS],
        "rubble": [[b.x0, b.y0, b.x1, b.y1, b.material] for b in layout.RUBBLE],
        "rooms": [{"name": r.name, "x": [r.x0, r.x1], "y": [r.y0, r.y1]} for r in layout.ROOMS],
        "shadow_room": layout.SHADOW_ROOM,
        "radio_sites": {k: list(s.gnb_m) for k, s in layout.RADIO_SITES.items()},
        "robots": list(layout.ROBOT_IDS), "ue_node": layout.UE_NODE,
        "grid": {"rows": grid.ROWS, "cols": grid.COLS, "res_m": grid.RESOLUTION_M, "x0": grid.X0, "y0": grid.Y0},
    }


class Feed:
    """Newest positions-feed frame, from a background SUB socket that follows current.json."""

    def __init__(self, fixed_endpoint: str | None):
        self.fixed = fixed_endpoint
        self.frame = None
        self.endpoint = None
        self.lock = threading.Lock()

    def follow(self, endpoint: str | None) -> None:
        endpoint = self.fixed or endpoint
        if endpoint and endpoint != self.endpoint:
            self.endpoint = endpoint
            threading.Thread(target=self._run, args=(endpoint,), daemon=True).start()

    def _run(self, endpoint: str) -> None:
        import zmq
        sub = zmq.Context.instance().socket(zmq.SUB)
        sub.setsockopt_string(zmq.SUBSCRIBE, "")
        sub.connect(endpoint)
        while self.endpoint == endpoint:
            if sub.poll(500):
                try:
                    frame = json.loads(sub.recv())
                except ValueError:
                    continue
                with self.lock:
                    self.frame = frame
        sub.close(0)


def read_json(path: pathlib.Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--run-dir", type=pathlib.Path, required=True, help="directory with current.json")
    ap.add_argument("--positions-endpoint", help="fixed positions feed (live gate); else from current.json")
    ap.add_argument("--radio-ui", default="", help="URL of the gate's shared web UI (radio panels)")
    args = ap.parse_args(argv)

    feed = Feed(args.positions_endpoint)
    layout_json = json.dumps(layout_body()).encode()
    index = (HERE / "index.html").read_bytes()

    def state() -> dict:
        current = read_json(args.run_dir / "current.json") or {}
        run = pathlib.Path(current.get("run_dir", args.run_dir))
        feed.follow(current.get("positions_endpoint"))
        with feed.lock:
            frame = feed.frame
        return {
            "current": current,
            "central": read_json(run / "central_state.json"),
            "links": {rid: read_json(run / f"link-{rid}.json") for rid in layout.ROBOT_IDS},
            "robots": {rid: read_json(run / f"robot-{rid}.json") for rid in layout.ROBOT_IDS},
            "referee": frame,
        }

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, body: bytes, kind: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self.send(index, "text/html; charset=utf-8")
            elif path == "/api/layout":
                self.send(layout_json, "application/json")
            elif path == "/api/state":
                self.send(json.dumps(state(), separators=(",", ":")).encode(), "application/json")
            elif path == "/api/radio":
                self.send(json.dumps({"radio_ui": args.radio_ui}).encode(), "application/json")
            elif path.startswith("/frames/"):
                name = path[len("/frames/"):]
                if name.removesuffix(".jpg") not in VIEWS or not name.endswith(".jpg"):
                    self.send_error(404)
                    return
                current = read_json(args.run_dir / "current.json") or {}
                frame = pathlib.Path(current.get("run_dir", args.run_dir)) / "frames" / name
                try:
                    self.send(frame.read_bytes(), "image/jpeg")
                except FileNotFoundError:
                    self.send_error(404)
            else:
                self.send_error(404)

    server = http.server.ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"rescue dashboard on http://{args.bind}:{args.port} (run dir {args.run_dir})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
