# v2x_intersection — V2X-assisted braking at a blind intersection

A closed-loop application demo of the GPU channel emulator. Two vehicles
approach an urban intersection whose corner building hides each from the
other. A roadside server sends vehicle B's state to vehicle A over the live
5G downlink; A's controller brakes or yields on it; A's resulting motion moves
A's UE in the Sionna RT scene, which changes the channel the emulator applies
to the IQ samples carrying the next message.

```
                    positions feed (ZMQ, 20 Hz)
  SUMO ── drive.py ──────────────────────────────┬──────────────▶ Sionna bridge ─▶ broker (GPU)
   ▲      (A's controller)                       │                (A = ue0)          │ channel
   │ accel                                       ▼                                   ▼
   │                                       roadside.py ── UDP 10 Hz ─▶ core ─▶ gNB ─▶ IQ ─▶ srsUE ue0
   │                                       (B's state)                                       │ TUN
   └──── unix socket (in-vehicle bus) ◀── ue_client.py (inside ue0's network namespace) ◀───┘
```

Two loops meet in the emulator. **Vehicle loop:** V2X message → UE →
controller → acceleration → SUMO → A's trajectory. **Channel loop:** A's
trajectory → Sionna RT → channel → emulator → the next V2X message. No
datagram reaches the controller except through the UE's TUN interface in the
live gate.

## External dependencies

Nothing below is in this repository or a submodule. `setup-external.sh`
installs the demo's own dependencies into a virtual environment outside the
tree; the radio stack and Sionna are the native workspace's.

| Dependency | Version | Licence | Where it lives | Used by |
| --- | --- | --- | --- | --- |
| [Eclipse SUMO](https://eclipse.dev/sumo/) (`eclipse-sumo`, `libsumo`, `sumolib` wheels) | 1.28.0 | EPL-2.0 | `~/venvs/v2x` (`setup-external.sh`) | `sumo_world.py` only (the one module that imports SUMO) |
| libX11, libXext, libXrender, libGL (Ubuntu 24.04 arm64 `.deb`s) | noble archive | MIT/X11 | `~/venvs/v2x/x11-libs`, only when the host lacks them | the SUMO wheel links them even headless; `setup-external.sh` unpacks them without root |
| pyzmq, numpy, matplotlib, pytest | `requirements.txt` | BSD / BSD / PSF-like / MIT | `~/venvs/v2x` | positions feed, analysis, tests |
| Sionna RT | the native workspace's | Apache-2.0 | `$OCUDU_NATIVE_SIONNA_PYTHON` | the bridge, `channel_profile.py` |
| OCUDU gNB, srsUE, Open5GS | pinned by the native gate | — | `$OCUDU_NATIVE_ROOT` | the live gate |

```
bash examples/v2x_intersection/setup-external.sh      # prints V2X_PYTHON=~/venvs/v2x/bin/v2x-python
```

Every script runs with `~/venvs/v2x/bin/v2x-python`, a wrapper the setup
script writes: it sets `SUMO_HOME`, puts SUMO's `bin/` on `PATH` and the
unpacked X11 libraries on `LD_LIBRARY_PATH`. `ue_client.py` and `messages.py`
are stdlib-only, so the UE namespace side runs on `/usr/bin/python3`.

## Files

| File | Role |
| --- | --- |
| `layout.py` | The intersection: blocks, lanes, conflict point, stop line, start positions, radio sites. The single source of geometry for SUMO, Sionna, the controller and the local sensor. |
| `sumo_world.py` | SUMO adapter (libsumo in-process). Builds the network from `layout.py`, disables SUMO's own collision avoidance, applies A's acceleration and B's scripted speed. |
| `controller.py` | A's controller, pure Python: time-to-conflict risk layer, yield latch, braking, stale-information fallback, PI speed control. |
| `messages.py` | Wire formats: V2X message (JSON), in-vehicle relay frame, positions-feed frame. |
| `drive.py` | One episode: SUMO + controller paced to the wall clock, publishes positions, reads relayed V2X messages; writes `vehicles.csv`, `v2x_rx.csv`, `result.json`. |
| `roadside.py` | Roadside server: B's state from the positions feed → UDP to A's UE at 10 Hz. |
| `ue_client.py` | Runs on A's UE: binds the UE address, stamps and logs each datagram, relays it to `drive.py`. |
| `run_local.py` | All of the above on 127.0.0.1 (no radio); `--bridge` adds the Sionna bridge in `--dry-run`. |
| `run-ocudu-v2x-intersection.sh` | Live gate: the native multi-UE Sionna gate with `launch/root_exec.sh` and `launch/ue_exec.sh` as its hooks. |
| `channel_profile.py` | Traces A's downlink along its lane exactly as the bridge does (Sionna venv). |
| `analyze.py`, `plot_run.py` | Per-episode join of vehicle, V2X, channel, UE and broker logs; figures. |
| `web/` | The V2X dashboard: `server.py`, `index.html` (built by `build_index.py` from the shared dashboard and `parts/`). See *Dashboard*. |
| `setup-external.sh`, `requirements.txt` | External install (see above). |
| `../configs/sionna/scenes/v2x_intersection/` | Scene and scenario generator; see its README. |
| `../../tests/test_v2x_*.py` | Wire formats (and the bridge's own parser on our feed), controller rules, geometry consistency (layout ↔ scene ↔ scenarios ↔ SUMO network), SUMO episodes, loopback closed loop. |

## Scenario

`layout.py`, scene frame = SUMO frame (x east, y north, metres, intersection
centre at the origin):

- Two-lane crossing roads (3.5 m lanes), four 62 × 62 × 18 m blocks whose
  faces stand 8 m from the road centre lines.
- **A** (ego, live UE `ue0`, antenna 1.5 m) drives north on x = 1.75 from
  y = −100 at 13.89 m/s (50 km/h). **B** (scripted) drives west on y = 1.75
  from x = 108 at 13.89 m/s. Undisturbed, both reach the conflict point
  (1.75, 1.75) together.
- The SE block hides B from A. A's **local sensor** is geometric ground truth:
  it sees B when the 2-D segment A–B misses every block and B is within 50 m,
  and reports B's approach *clear* when it can see the approach lane 30 m east
  of the conflict point and B is not on it. In this geometry that happens only
  in the last ~15 m before A's stop line.
- **Roadside server** = a *simulated roadside sensing proxy*: it forwards
  SUMO's ground truth for B, not a sensor detection.
- **Radio**: one gNB, `ue0` on A, `ue1` parked in line of sight of the gNB
  with no application traffic (the multi-UE gate runs two UEs). Two sites
  (`OCUDU_NATIVE_V2X_SITE`): `los` keeps A in line of sight; `blocked` (default)
  loses it behind the SW block ~49 m before the conflict point, i.e. while the
  warning about B matters.

SUMO's own safety logic is off (junction `unregulated`, speed mode 32, lane
change mode 0): only A's controller avoids B.

## Controller

Every 50 ms (`controller.py`):

1. **Estimate of B**, in priority: the local sensor sees B; a V2X message no
   older than 0.3 s (three periods), extrapolated at constant velocity from its
   simulation time to now; the sensor sees B's approach empty; otherwise none
   (`missing` / `stale` / `off`).
2. **Risk**: T_A = d_A / v_A and T_B = d_B / v_B to the conflict point; a
   conflict when B heads for the crossing (lateral offset < 3 m) and
   |T_A − T_B| < 2.5 s within a 10 s horizon, or B is in the crossing. Once
   flagged, A keeps yielding until B is 6 m past the conflict point.
3. **Action**: WARNING lowers the target speed so A arrives after B clears;
   if that needs more than half the comfortable deceleration (3 m/s²), BRAKING
   stops A at the stop line (deceleration capped at 6 m/s², the limit SUMO
   also applies), then STOP. Past the stop line while not braking for B, A
   drives on (PASSED); past it while braking for B, A keeps braking.
4. **No information**: fallback `cautious` (default) caps the speed at the one
   from which A can still stop at the line with 3 m/s²; `none` drives on.

A PI loop tracks the target speed. SUMO keeps A on its lane; there is no
steering command (`steering` is logged as empty).

## Wire formats

- **V2X message** (UDP, `messages.encode`): `{"message_type": "vehicle_state",
  "vehicle_id": "B", "sequence", "simulation_time", "send_wall_time_ns",
  "position": {x, y, z}, "velocity": {vx, vy, vz}}`, ~200 bytes;
  `send_wall_time_ns` is taken immediately before `sendto`.
- **Relay frame** (unix datagram, UE client → `drive.py`): 8-byte LE receive
  time (unix ns at the UE socket) + the datagram unchanged.
- **Positions feed** (ZMQ PUB, `drive.py`): the bridge's `positions` message
  (`scripts/sionna_rt/run_bridge.py`, `ExternalPositionSource`) with A's antenna
  as node `ue0`, plus `sim_time_s` and a `vehicles` object (A and B) the bridge
  ignores and the roadside server reads. `frame` is `arena` (the only frame the
  bridge accepts); here it equals the scene frame, offset 0.

Roadside server, UE client, `drive.py` and the bridge run on one host, so every
one-way delay below uses one clock.

## Timing

`drive.py` advances SUMO by 50 ms per step and sleeps to keep simulation time
equal to elapsed wall time; it never waits for a message or a channel update.
`result.json` records the real-time factor, late steps and the largest lag.
The bridge traces at up to 10 Hz on the newest position (latest-wins) and the
broker applies each batch at the next slot without blocking the IQ path. The
analysis reports, per channel update, *position-to-ACK*: from the feed's
publish time of A's position to the broker's acknowledgement of the channel
built from it.

## Running

No radio (loopback; the reference, not an integrated result):

```
~/venvs/v2x/bin/v2x-python examples/v2x_intersection/run_local.py \
    --out results/v2x-local/<ts> --modes v2x,no-v2x,v2x:none
# + the SUMO -> Sionna half of the loop, no broker:
~/venvs/v2x/bin/v2x-python examples/v2x_intersection/run_local.py --out results/v2x-local/<ts> \
    --modes v2x --bridge examples/configs/sionna/scenarios/v2x_intersection/v2x-intersection-blocked.json
```

Live (from the repo root, as root, with the native workspace's environment:
`OCUDU_NATIVE_ROOT`, `OCUDU_NATIVE_SIONNA_PYTHON`, ...):

```
OCUDU_NATIVE_V2X_SITE=blocked OCUDU_NATIVE_V2X_MODES=v2x,v2x:none,no-v2x \
  bash examples/v2x_intersection/run-ocudu-v2x-intersection.sh
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `OCUDU_NATIVE_V2X_SITE` | `blocked` | `blocked` or `los` scenario. |
| `OCUDU_NATIVE_V2X_MODES` | `v2x,no-v2x` | Cycled per episode. `v2x` / `no-v2x` (controller ignores V2X), optionally `:cautious` / `:none` for the fallback. |
| `OCUDU_NATIVE_V2X_EPISODES` | `0` | Episodes, 0 = back to back until the gate ends. |
| `OCUDU_NATIVE_V2X_DRIVE_EXTRA` | — | Extra `drive.py` words for every episode (e.g. `--b-start-x 112`). |
| `OCUDU_NATIVE_MUE_DURATION_SECONDS` | `420` | Broker run length; attach takes 1–2 min, each episode ~13 s. |
| `OCUDU_NATIVE_SIONNA_TAP_DOPPLER_BIN_HZ` | — | Opt-in per-tap Doppler (`1` = split delay bins at 1 Hz): taps rotate between Sionna updates; see [the plan](../../docs/plans/sionna-tap-doppler.md). Not yet validated live. |
| `OCUDU_NATIVE_SIONNA_AWGN_SNR_DB` | `40` | The multi-UE gate's noise floor relative to a 0 dB tap: the link budget knob for the degraded runs below. |
| `OCUDU_NATIVE_V2X_WEB_PORT` / `OCUDU_NATIVE_WEB_PORT` | `8080` / `8081` | V2X dashboard / the shared web UI it proxies (`0` = no V2X dashboard). |
| `V2X_PYTHON` | `~/venvs/v2x/bin/v2x-python` | The external interpreter. |

The gate's own verdict (`attach-summary.json`) still judges attach, PCI and
transport; the V2X files land in `<log dir>/v2x/` (`roadside_tx.csv`,
`ue_rx.csv`, `episodes/eNNN-<mode>/`). Then:

```
~/venvs/v2x/bin/v2x-python examples/v2x_intersection/analyze.py <log dir> --out <dir>
~/venvs/v2x/bin/v2x-python examples/v2x_intersection/plot_run.py <log dir> --out <dir>
```

## Dashboard

The gate starts the V2X dashboard on `http://127.0.0.1:8080`
(`OCUDU_NATIVE_V2X_WEB_PORT`) and moves the shared web UI to 8081
(`OCUDU_NATIVE_WEB_PORT`). `web/server.py` serves `web/index.html`, proxies the
shared UI's `/api/status`, `/api/realtime` and `/api/scene_mesh`, and adds:

- `/api/road`: SUMO's own lane and junction shapes for the layout's network
  (sumolib), the stop lines and the building blocks;
- `/api/vehicles`: the newest positions-feed frame (both vehicles, 20 Hz);
- `/api/v2x`: the running episode's last rows of `vehicles.csv` and
  `v2x_rx.csv` (flushed every step) and the last twelve results.

Panels: the scene (the SUMO network in 3D: lanes, markings, stop lines,
crossings, buildings, both vehicles with brake lights, the gNB, the rays of
gnb0 → ue0 and A's trail coloured by the strongest DL tap), timing, vehicle
state, vehicle dynamics and V2X reception, node and antenna status, system
resources and the Sionna runtime loop, estimated nominal-slot deadline
utilization, impulse and frequency response with tap details. The radio panels
are the shared dashboard's rendering code, taken unchanged by
`web/build_index.py`, which assembles `web/index.html` from
`scripts/web_ui/index.html` and `web/parts/`; rerun it after changing either.

SUMO's own viewer, `sumo-gui`, is a desktop application (X11, optional
OpenSceneGraph 3D) and cannot be embedded in a web page; the dashboard draws
the same network geometry itself.

Standalone (a running gate's shared UI on 8081, or a `run_local.py` output):

```
~/venvs/v2x/bin/v2x-python examples/v2x_intersection/web/server.py --port 8080 \
    --upstream http://127.0.0.1:8081 --positions-endpoint ipc://$OCUDU_NATIVE_ROOT/run/v2x/positions.sock \
    --logs-root $OCUDU_NATIVE_ROOT/results/logs/ocudu-multi-ue
```

## Measured results (GB10, 2026-10-12)

Platform: DGX Spark GB10 container, OCUDU gNB + srsUE (`srsran4g-zmq-local`)
+ Open5GS through the native multi-UE gate (`spark-gb10` profile, strict
real-time off), 23.04 MS/s, 15 kHz SCS (1 ms slots), one gNB and two
single-port UEs, Sionna RT channel (`sionna_rt` on all four links), CUDA
broker. Each run: 420 s broker time, ~28 episodes of ~13 s cycling `v2x`,
`v2x:none`, `no-v2x`; B starts at x = 108 m in every episode. Logs:
`$OCUDU_NATIVE_ROOT/results/logs/ocudu-multi-ue/<run>/`; numbers from
`analyze.py`. The last episode of each run overlaps the gate's teardown and
shows partial delivery for that reason.

**Vehicle outcome** (minimum centre distance between A and B; episodes per mode 9–10):

| Run (site, noise floor) | Gate verdict | `v2x` | `v2x:none` | `no-v2x` |
| --- | --- | --- | --- | --- |
| `los`, 40 dB (`20261012T084519Z`) | pass, ue0 no RLF/re-attach | 29.0–29.1 m, 0 collisions | 29.0–29.1 m, 0 | 12.1 m, 0 |
| `blocked`, 40 dB (`20261012T082928Z`) | pass, ue0 no RLF/re-attach | 29.0–29.3 m, 0 | 29.1–29.3 m, 0 | 12.1 m, 0 |
| `blocked`, 34 dB (`20261012T083701Z`) | fail: ue0 14 re-attach attempts | e000–e010: 28.6–29.0 m, 0; after the link loss 12.1 m, 0 | e000–e010: 28.9–29.0 m, 0; after the link loss **3.5 m, 6/6 collisions** | 12.1 m, 0 |

- With V2X, A's controller flags the conflict 0.15–0.45 s into the episode
  (the first messages), slows to ~9 m/s and brakes once more near the stop
  line; the two never come closer than ~29 m. Without V2X the local sensor
  first sees B 6.55 s in, when A is 15.9 m from the conflict point (6.1 m
  before its stop line); the `cautious` fallback has already slowed A to
  8.6 m/s by then, so it stops in time and the closest approach is 12.1 m. A
  lockstep SUMO episode without that fallback (`--no-v2x --fallback none`)
  collides (`tests/test_v2x_episode.py`).
- **The chain from channel to vehicle is visible only in the 34 dB run.**
  Behind the SW block the DL strongest tap falls to −34.8 dB; at 40 dB the UE
  keeps the link (DL SNR ≥ 7.3 dB, BLER ≤ 47 %, delivery 100 %, one-way p50
  7.6 ms, max 258 ms, longest reception gap 350 ms), so V2X arrives in time in
  every episode and the outcome does not change. At 34 dB the same geometry
  takes BLER up to 98 %: for 11 episodes 99–100 % of the messages still
  arrive, some up to 3.8 s late (dropped by the controller's 0.3 s age limit; A had decided
  to yield earlier, in line of sight), then ue0 loses the link in e011 and
  does not re-attach for the rest of the run. From there `v2x` falls back to
  `cautious` and matches `no-v2x`; `v2x:none` drives into the crossing
  every time. A 30 dB run (`20261012T043617Z`, before the controller's yield
  latch; link figures only) lost ue0 the same way after its first episode.
- The UE not recovering is the existing srsUE limit for a moving UE after a
  deep fade (`AGENT_PROGRESS.md`, current capabilities), not a demo
  mechanism; the gate marks those runs failed.

**Channel loop** (every run): the bridge followed A's live position (source
`external`) at ~10 updates/s (about 116 per episode); one trace takes ~49 ms
(p50). Position-to-ACK — from the feed's publish time of A's position to the
broker's ACK of the channel built from it — is 70–86 ms (median of episode
p50s), episode p95 at most 141 ms. A at 13.9 m/s moves ~1 m in that time.

**Emulator** (broker log, per run):

| Run | GPU H2D+kernel+D2H, per-second average (p50 / p99 / max) | Starvations (ue0 port) | UE port slot rate |
| --- | --- | --- | --- |
| `los`, 40 dB | 43 / 262 / 746 µs | 27 | 948 /s |
| `blocked`, 40 dB | 41 / 144 / 196 µs | 25 | 948 /s |
| `blocked`, 34 dB | 41 / 217 / 242 µs | 219 | 941 /s |

The broker log holds one average per second, not per-slot completion times,
so `analyze.py` cannot compute a per-slot deadline-miss ratio from it; the
per-second averages stay below the 1 ms slot in every second of every run. The
live dashboard's *estimated nominal-slot deadline utilization* (broker
telemetry, `sample_proportional_call_time`) does estimate it per slot: a
snapshot of the 2026-10-13 one-hour `blocked`, 40 dB run (~2.0 M nominal slots
per node) showed 1.9 % estimated misses on ue0 RX and 4.6 % on gnb0 RX, p95
253–872 µs, max 9.8–12.7 ms. These runs do not record that telemetry, so the
table above has no per-slot column. The radio ran at ~0.95 of
real time (948 of 1000 slots/s) and every run had rx starvations, so these
runs do not meet the strict real-time gate. Each channel batch whose tap
delays change re-warms the edge's history (`control_warmup`, ~6 400 per run
for ~4 190 batches): the known limit that geometry changes are not seamless.

## Limits

- One live UE moves (A); B carries no UE. `ue1` is parked and idle.
- Only the downlink carries application data. UDP over the default bearer;
  late delivery under deep fades (up to 3.8 s at 34 dB) is the RAN's
  behaviour, not injected.
- Simulation time is paced to wall time on one host. `drive.py` reports
  `rtf` 1.004 and zero late steps in every live episode; the radio itself runs
  at ~0.95 of real time.
- The local sensor and the roadside server read SUMO ground truth; neither
  models perception.
- The controller is a rule-based demonstration, not a safety function.
- The live verdict comes from the shipped multi-UE gate; the V2X demo adds no
  pass/fail rule of its own.
