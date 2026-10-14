#!/usr/bin/env python3
"""Assemble web/index.html from the shared dashboard and the V2X parts.

    python3 examples/v2x_intersection/web/build_index.py

Taken from `scripts/web_ui/index.html` unchanged: the stylesheet and the
classic script that renders the status panels (timing, node and antenna
status, system resources and the Sionna runtime loop, nominal-slot deadline
utilization, impulse/frequency response and taps). Its `$()` lookup is the
one edit: ids of panels this page leaves out resolve to a detached element,
so the shared render() runs unmodified. The status poll drops the telemetry
history it does not need (see main()). Replaced: the header, the panel
markup, and the scene module (parts/scene_module.js: the SUMO network in
3D). Added: vehicle state tiles beside the timing (parts/v2x_side.html) and the
vehicle charts and episode table (parts/v2x_panel.*). Rerun after changing the
shared dashboard or a part; the output is committed so the server needs no
build step.
"""

from __future__ import annotations

import pathlib

HERE = pathlib.Path(__file__).resolve().parent
SHARED = HERE.parents[2] / "scripts" / "web_ui" / "index.html"
PARTS = HERE / "parts"
LOOKUP = "const $ = id => document.getElementById(id);"
SINK_LOOKUP = (
    "// Panels the V2X page leaves out write into one detached element.\n"
    "    const $sink = document.createElement('div');\n"
    "    const $ = id => document.getElementById(id) || $sink;\n"
    "    // Replaced by the scene module once it has loaded (it runs after this script).\n"
    "    window.sceneViewer = window.sceneViewer || { update() {}, resize() {} };"
)

CSS = """
    .h2-sub { color:var(--muted); text-transform:none; letter-spacing:0; font-weight:400; }
    .scene-stage.v2x { border-color:#26394a; background:#bfd3e3; }
    #scene3d { height:660px; }
    .scene-tap { position:absolute; left:12px; top:12px; padding:5px 9px; border-radius:8px; background:#081019cc; color:#ffd27a; font-size:12px; }
    .scene-legend-bar { display:inline-block; width:90px; height:8px; border-radius:4px; vertical-align:middle;
      background:linear-gradient(90deg,hsl(40,90%,18%),hsl(40,90%,78%)); margin:0 6px; }
    .v2x-head { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:12px; }
    .v2x-episode b { font-size:16px; display:block; } .state-chip { padding:6px 14px; border-radius:99px; font-weight:700; letter-spacing:.06em; }
    .v2x-stats { grid-template-columns:repeat(2,minmax(0,1fr)); }
    .v2x-charts { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin-top:14px; }
    .v2x-charts figure { margin:0; padding:10px; border:1px solid #20323f; border-radius:9px; background:#07111a; }
    .v2x-charts figcaption { color:var(--muted); font-size:12px; margin-bottom:6px; }
    .v2x-charts canvas { height:200px; border:0; background:transparent; }
    .key { display:inline-block; width:14px; height:3px; border-radius:2px; vertical-align:middle; margin:0 4px 0 6px; }
    .key-a { background:#3987e5; } .key-b { background:#d95926; } .key-target { background:repeating-linear-gradient(90deg,#c3c2b7 0 4px,transparent 4px 7px); }
    .state-legend { display:flex; gap:14px; margin:10px 0; color:var(--muted); font-size:12px; }
    .state-legend i { display:inline-block; width:12px; height:8px; border-radius:2px; margin-right:5px; }
    .bad-cell { color:var(--red); font-weight:700; }
    /* Explanatory notes the shared renderer writes inside its panels: not shown here. */
    .memory-explain { display:none; }
    @media (max-width:1100px) { .v2x-charts { grid-template-columns:1fr; } .v2x-stats { grid-template-columns:repeat(2,minmax(0,1fr)); } }
"""

HEADER = """  <header>
    <div><h1>V2X Intersection · Live Channel Emulation</h1><div class="sub">SUMO vehicles · Sionna RT channel · OCUDU gNB / srsUE through the GPU channel emulator</div></div>
    <div class="pills"><span id="sionnaPill" class="pill bad">Sionna waiting</span><span id="telemetryPill" class="pill bad">Telemetry waiting</span><span id="controlPill" class="pill bad">Control ACK waiting</span><span id="backendPill" class="pill bad">Backend waiting</span><span id="updatedPill" class="pill">—</span></div>
  </header>
"""

PANELS_TOP = """  <main>
    <div class="grid">
      <section class="panel">
        <h2>Scene · SUMO network, Sionna rays</h2>
        <div class="scene-stage v2x">
          <div id="scene3d"></div>
          <div class="scene-toolbar">
            <button type="button" id="sceneRaysToggle">Rays</button>
            <button type="button" id="sceneLabelsToggle">Labels</button>
            <button type="button" id="sceneTrailToggle">Trail</button>
            <button type="button" id="sceneFollowToggle">Follow A</button>
            <button type="button" id="sceneResetView">Reset view</button>
          </div>
          <div class="scene-hint">Drag to orbit · scroll to zoom · right-drag to pan</div>
          <div class="scene-status" id="sceneStatus">Loading SUMO network…</div>
          <div class="scene-credit" id="sceneCredit"></div>
          <div class="scene-tap" id="sceneTap">DL strongest tap to A —</div>
        </div>
        <div class="legend"><span><i class="dot" style="background:#3987e5"></i>Vehicle A · UE ue0</span><span><i class="dot" style="background:#d95926"></i>Vehicle B</span><span><i class="dot" style="background:#ff3b30"></i>gNB</span><span>Rays gnb0 → ue0, brighter = stronger</span><span>A's trail: DL tap<i class="scene-legend-bar"></i>weak → strong</span></div>
      </section>
      <section class="panel">
        <h2>Timing</h2>
        <div class="stats">
          <div class="stat"><b id="iteration">—</b><span>Sionna iteration</span></div>
          <div class="stat"><b id="generation">—</b><span>Channel generation ms</span></div>
          <div class="stat"><b id="controlTime">—</b><span>Send + ACK ms</span></div>
          <div class="stat"><b id="generateToAck">—</b><span>Generation start → ACK ms</span></div>
          <div class="stat"><b id="totalUpdate">—</b><span>Total update ms</span></div>
          <div class="stat"><b id="freq">—</b><span>DL / UL MHz</span></div>
        </div>
"""

PANELS_TOP_END = """      </section>
"""

PANELS_BOTTOM = """      <section class="panel wide">
        <h2>Node and antenna status</h2>
        <div id="nodeCards" class="node-grid"><div class="empty">Waiting for node data</div></div>
      </section>
      <section class="panel wide">
        <h2>System resources · Sionna runtime loop</h2>
        <article id="combinedGpuCard" class="deadline-card"></article>
        <div class="delivery" style="margin-top:12px;margin-bottom:0">
          <strong id="runtimeTitle" class="wait">Waiting for Sionna runtime</strong>
          <div id="runtimeDetail" class="sub">Waiting for the runtime PID and current processing phase.</div>
          <div class="loop-flow"><span>Initialize scene once</span><i>→</i><span>Recompute channels</span><i>→</i><span>Send profile batch + ACK</span><i>→</i><span>Wait for next update</span><i>↺</i></div>
          <div id="runtimeConfig" class="gpu-lines"></div>
        </div>
      </section>
      <section class="panel wide">
        <h2>Estimated nominal-slot deadline utilization by receiving node</h2>
        <div id="deadlineCards" class="deadline-grid"></div>
      </section>
      <section class="panel wide">
        <h2>Impulse and frequency response by edge link · Tap details</h2>
        <div id="edgeCards" class="edge-grid"></div>
      </section>
    </div>
  </main>
"""


def main() -> int:
    shared = SHARED.read_text(encoding="utf-8")
    head_end = shared.index("  </style>")
    head = shared[:head_end].replace("<title>OCUDU Sionna Channel Status</title>",
                                     "<title>V2X Intersection Live</title>")
    script_start = shared.index("  <script>\n    const $ = id")
    script_end = shared.index("  </script>", script_start) + len("  </script>\n")
    classic = shared[script_start:script_end]
    assert LOOKUP in classic, "shared dashboard changed its $() lookup"
    classic = classic.replace(LOOKUP, SINK_LOOKUP, 1)
    # The 500 ms status poll asks for no telemetry history (~60 KB instead of
    # ~5 MB per poll) and no longer redraws the resource charts: pollCharts()
    # already redraws them once a second from /api/realtime, and each redraw of
    # the 500 Hz series stalls the main thread (and the 3D scene) for a while.
    for old, new in (
        ("fetch(`/api/status?history_ms=${CHART_HISTORY_MS}`", "fetch(`/api/status?history_ms=0`"),
        ("      drawResourceHistory(usage,history,runtime,sionna);\n",
         "      // Resource charts: redrawn by pollCharts() only (V2X page).\n"),
    ):
        assert old in classic, f"shared dashboard changed: {old!r}"
        classic = classic.replace(old, new, 1)
    page = (
        head + CSS + "  </style>\n</head>\n<body>\n" + HEADER + PANELS_TOP
        + (PARTS / "v2x_side.html").read_text(encoding="utf-8") + PANELS_TOP_END
        + (PARTS / "v2x_panel.html").read_text(encoding="utf-8") + PANELS_BOTTOM
        + classic + (PARTS / "v2x_panel.js").read_text(encoding="utf-8")
        + (PARTS / "scene_module.js").read_text(encoding="utf-8") + "</body>\n</html>\n"
    )
    (HERE / "index.html").write_text(page, encoding="utf-8")
    print(f"wrote {HERE / 'index.html'} ({len(page)} bytes) from {SHARED}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
