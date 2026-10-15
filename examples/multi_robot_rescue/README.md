# multi_robot_rescue — three rescue robots, a mission server and the live 5G link

A closed-loop application demo of the GPU channel emulator. Three ground
robots search an unknown, partly collapsed building for two survivors. Each
robot explores on its own sensors and its own map; a mission server behind the
gNB fuses what the robots send, assigns frontiers and registers survivor
reports. Every message between a robot and the server crosses that robot's
UE, the channel emulator and the gNB; the robots' positions move their UEs in
the Sionna RT scene, which changes the channel the emulator applies to the IQ
samples carrying the next message.

```
                         positions feed (ZMQ, 10 Hz)
  sim.py (world, referee) ─────────────────────────────┬──────────▶ Sionna bridge ─▶ broker (GPU)
   ▲  │ sensor frames (unix)                            │             (robot i = ue{i-1})   │ channel
   │  ▼                                                 ▼                                   ▼
  cmd  robot.py ── events (unix) ──▶ ue_link.py ══ UDP over TUN ══ srsUE ═ IQ ═ broker ═ gNB ═ core
       (local autonomy)  ◀── tasks ── (UE namespace)                                         │
                                                                        mission_server.py ◀──┘
                                                                        (10.45.1.1, behind the UPF)
```

**Shadow-region policy** (the user's decision, fixed): a robot whose link is
lost keeps exploring on its own — it does not turn back for coverage, wait,
or stop. Its UE link keeps trying to reconnect in the background and, once the
server answers again, delivers what the robot recorded meanwhile, survivor
reports first. Delivery, not radio quality, decides the link state.

Read [`MULTI_ROBOT_RESCUE_MILESTONES.md`](../../MULTI_ROBOT_RESCUE_MILESTONES.md)
for the audit, plan, measured results and what is and is not verified.

## Terms

| Term | Meaning here |
| --- | --- |
| **link state** | The UE link's view of the application path to the server: `BOOT`, `ONLINE`, `DEGRADED` (no valid reply for 3 s), `OFFLINE` (5 s), `SYNCING` (server answers again, backlog not yet acknowledged). Measured from replies; never from SNR. |
| **backlog** | Events stored in the robot's offline queue and not yet acknowledged by the server. |
| **local confirmation** | A robot's own decision that a survivor is there (three consistent detections). |
| **central report** | The mission server's first receipt of that survivor report. A different timestamp and a different metric from local confirmation. |
| **perception mock** | The geometric stand-in for a camera detector (`sensors.detect_survivors`): bearing and range of a survivor in view with clear line of sight. No identity; a development mock. |
| **network mode** | What sits between UE link and server: `gpu_iq_live_5g` (the live gate), `ideal_network_baseline`, `simple_loss_delay_baseline`, `mock_network_development` (fault injection). Recorded in every episode's `run.json` and shown on the dashboard. |
| **world** | `kinematic` (2-D kinematic robots, `kinematic_world.py`, development) or `isaac` (Isaac Sim, `isaac_world.py`). |

## External dependencies

Nothing below is in this repository or a submodule. `setup-external.sh`
installs the demo's own dependencies into a virtual environment outside the
tree; the radio stack and Sionna are the native workspace's.

| Dependency | Version | Licence | Where it lives | Used by |
| --- | --- | --- | --- | --- |
| NVIDIA Isaac Sim (`isaacsim[all,extscache]` wheels, pypi.nvidia.com) | 6.2.0.0 | NVIDIA Isaac Sim EULA (accept with `OMNI_KIT_ACCEPT_EULA=YES`) | `~/venvs/rescue` (`setup-external.sh --isaac`, ~20 GB) | `isaac_world.py` only (the one module that imports Isaac Sim) |
| Python 3.12 headers, X11/GL client libraries (Ubuntu 24.04 arm64 `.deb`s) | noble archive | PSF / MIT-X11 | `~/venvs/rescue-syslibs`, only when the host lacks them | building two Isaac dependencies from source; the NVIDIA Vulkan ICD at run time |
| numpy, pyzmq, pytest | `requirements.txt` | BSD / BSD / MIT | `~/venvs/rescue` | everything except the UE side |
| Sionna RT | the native workspace's | Apache-2.0 | `$OCUDU_NATIVE_SIONNA_PYTHON` | the bridge, `channel_profile.py` |
| OCUDU gNB, srsUE, Open5GS | pinned by the native gate | — | `$OCUDU_NATIVE_ROOT` | the live gate |

```
bash examples/multi_robot_rescue/setup-external.sh                 # prints RESCUE_PYTHON=~/venvs/rescue/bin/rescue-python
OMNI_KIT_ACCEPT_EULA=YES bash examples/multi_robot_rescue/setup-external.sh --isaac   # + Isaac Sim
```

`ue_link.py`, `offline_queue.py` and `messages.py` are stdlib-only, so the UE
namespace side runs on `/usr/bin/python3`. ROS 2 / Nav2 is not used: it needs
system packages (apt) this host does not allow, and the robots' processes then
have no network socket at all, so no middleware discovery can bypass the 5G
path. Mapping, frontier choice and path following are the demo's own
(`grid.py`, `robot.py`).

## Files

| File | Role |
| --- | --- |
| `layout.py` | The building: walls (with materials), rubble, rooms, the steel-lined shadow room N4, robot spawns, survivor presets, radio sites. The single source of geometry for both worlds, the Sionna scene, the referee and the dashboard. |
| `messages.py` | Wire formats: event envelope, application datagrams (≤ 1200 B), tasks and their acceptance rules, map-tile codec, positions-feed frame. |
| `kinematic_world.py`, `sensors.py` | Development world: kinematic differential-drive robots, 2-D LiDAR by ray casting, perception mock. |
| `isaac_world.py` | Isaac Sim adapter with the same interface (see *Isaac Sim*). |
| `sim.py` | One episode's world process: physics paced to the wall clock, body bus per robot, positions feed, referee (coverage of the central map, survivor matching), `result.json`. |
| `grid.py` | Occupancy grids (0.2 m), 16 × 16-cell map tiles, frontiers, wavefront planning. |
| `robot.py` | One robot's local autonomy: mapping, frontier exploration, central-task following, survivor confirmation, reactive safety layer. Unix sockets only. |
| `offline_queue.py` | Durable, bounded, priority-ordered event queue (SQLite). |
| `ue_link.py` | One robot's UE link: link state machine, heartbeats and `resume_hello` probes with bounded backoff, windowed ACK-based delivery, task relay. |
| `mission_server.py`, `planner.py` | Central side: deduplication and ACKs, map fusion, robot status, survivor registry, deterministic frontier allocation with commitment. |
| `link_proxy.py` | No-radio impairment proxy for the loopback baselines (fault injection, delay/loss, shadow-room loss). Never in the live gate. |
| `run_local.py` | All of the above on 127.0.0.1 (no radio), per network mode; `--bridge` adds the Sionna bridge in `--dry-run`. |
| `run-ocudu-multi-robot-rescue.sh`, `launch/` | Live gate: the native multi-UE Sionna gate with `launch/root_exec.sh` and `launch/ue_exec.sh` as its hooks. |
| `channel_profile.py` | Traces gNB ↔ robot over the floor exactly as the bridge does (Sionna venv); per-room summary. |
| `analyze.py` | Per-episode metrics: `summary.json`, `summary.csv`. |
| `web/` | The dashboard: `server.py`, `index.html`. |
| `setup-external.sh`, `requirements.txt` | External install (see above). |
| `../configs/sionna/scenes/multi_robot_rescue/` | Scene and scenario generator. |
| `../../tests/test_rescue_*.py` | Wire formats, offline queue, link state machine, grids and fusion, layout ↔ scene ↔ scenarios, information separation, outage recovery through real processes, one loopback episode. |

## Scenario

Site frame = scene frame (x east, y north, metres, origin at the building's
outer south-west corner): a 40 × 24 m one-storey building, a 4 m corridor along
x with eight 10 × 10 m rooms (S1–S4 south, N1–N4 north), one 1.2 m door per
room, the entrance an open 3 m doorway in the west wall, a 3 m concrete roof.
Exterior walls concrete, corridor walls brick, partitions plasterboard; **N4**
is lined with steel (metal walls, door at x = 32). Rubble narrows the corridor
to ~1.4 m at x ≈ 24 and blocks parts of every room.

- Robots `robot_1..3` start inside the entrance facing east (0.25 m radius,
  ≤ 0.6 m/s, antenna 0.35 m). Robot i carries UE `ue{i-1}`.
- Survivors: preset `demo` puts one deep in N4 and one in S2; `random` draws
  two rooms by seed. Robots learn of a survivor only through the perception
  mock; the server only through a delivered report.
- Radio sites (`layout.RADIO_SITES`): `entrance` (default) — a portable gNB on
  a tripod just outside the doorway at (−1, 12, 2.5); `street` — 12 m west at
  4 m. Strongest DL tap after the bridge's `--gain-offset-db 60`
  (`channel_profile.py`, 2 m grid, median per area): `entrance`: corridor
  −0.4 dB, near rooms −9…−14 dB, far rooms −23…−26 dB, **N4 no path
  (−100 dB floor) except next to its door**; `street`: corridor −4 dB, rooms
  −23…−34 dB, S4 and N4 no path.

## Isaac Sim

`--world isaac` (sim.py, run_local.py, `OCUDU_NATIVE_RESCUE_WORLD`) replaces
the kinematic world with `isaac_world.py`: the same building as PhysX
FixedCuboids, survivors as 0.6 × 0.6 × 0.35 m boxes, three robots built from
primitives (chassis, two driven wheels, caster) driven by Isaac's
`DifferentialController`, and one `Raycast` 2-D LiDAR per robot. Headless,
`render=False`; physics at sim.py's 50 Hz. Measured on the GB10: start-up
6–8 s, 1.7 ms per step for three robots with LiDAR (RTF ≈ 12 alone, 1.000 in
an episode, which is wall-clock paced), LiDAR equal to the kinematic ray
caster to the beam (median difference 0).

- Rays start 0.27 m out from the robot centre: a ray starting inside the
  robot's own chassis hits it (every beam read 0.05 m). The LiDAR is at
  chassis height, so the robots see each other.
- The NVIDIA robot assets come only from an internet asset server
  (`get_assets_root_path()`), so the robots are built from primitives.
- Survivor perception is the same geometric mock; contacts are not counted.
- `rescue-python` sets what Isaac needs on this host: `OMNI_KIT_ACCEPT_EULA`
  (accepted by the user at install), `LD_PRELOAD` of libgomp (Kit exits
  without it) and the user-space X11/GL libraries (without them Isaac starts
  but cannot create its Vulkan device).

## Local autonomy and the central side

Every sensor frame (10 Hz) the robot integrates its scan into its log-odds
grid and associates detections into survivor candidates; three consistent
hits confirm one. Every second it (re)plans: a valid central task (newer
revision, before `valid_until_ms`, target reachable in its own map with
unknown space allowed, not one it just reached or got stuck on) is followed;
otherwise it takes the cheapest reachable frontier (path length − 0.3 m per
frontier cell). A reactive layer stops forward motion when the robot-wide lane
ahead is blocked and backs up when blocked for 1.5 s. The link state never
enters these decisions.

The server answers every datagram, acknowledges every event id (new,
duplicate or from another mission), fuses tiles newest-first per robot,
registers survivors (reports within 1.5 m merge) and every 2 s gives each
ONLINE robot without a live task the cheapest frontier not near another
robot's target or an offline robot's last position. A task the robot accepted
is kept while its target is still a frontier and renewed before it expires;
targets a robot refused (stuck, unreachable) are not offered to it again for
60 s. Offline robots are sent nothing; their last pose is shown with its age.

## Running

No radio (loopback; references, not radio results):

```
PY=~/venvs/rescue/bin/rescue-python
$PY examples/multi_robot_rescue/run_local.py --out results/rescue-local/<ts> --network ideal
$PY examples/multi_robot_rescue/run_local.py --out results/rescue-local/<ts> --network blackout --blackout "robot_2:40-60"
$PY examples/multi_robot_rescue/run_local.py --out results/rescue-local/<ts> --network shadow     # N4 = total loss
# dashboard for a local run:
$PY examples/multi_robot_rescue/run_local.py ... --web-status-dir /tmp/rescue-web &
$PY examples/multi_robot_rescue/web/server.py --port 8080 --run-dir /tmp/rescue-web
```

Live (from the repo root, as root, with the native workspace's environment:
`OCUDU_NATIVE_ROOT`, `OCUDU_NATIVE_SIONNA_PYTHON`, ...):

```
OCUDU_NATIVE_RESCUE_ROBOTS=1 bash examples/multi_robot_rescue/run-ocudu-multi-robot-rescue.sh   # MR3: one robot
OCUDU_NATIVE_RESCUE_ROBOTS=3 bash examples/multi_robot_rescue/run-ocudu-multi-robot-rescue.sh   # MR4: three robots, 4 UEs
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `OCUDU_NATIVE_RESCUE_ROBOTS` | `3` | 1, 2 or 3 robots on live UEs; 1–2 use the 2-UE gate, 3 the 4-UE gate (spare UEs parked outside). |
| `OCUDU_NATIVE_RESCUE_SITE` | `entrance` | Radio site (scenario JSON). |
| `OCUDU_NATIVE_RESCUE_EPISODES` | `1` | Episodes, 0 = back to back until the gate ends. |
| `OCUDU_NATIVE_RESCUE_WORLD` | `kinematic` | `kinematic` or `isaac`. |
| `OCUDU_NATIVE_RESCUE_SURVIVORS` | `demo` | Survivor preset or `random`. |
| `OCUDU_NATIVE_RESCUE_TIME_LIMIT` | `600` | Seconds per episode; it ends earlier on success. |
| `OCUDU_NATIVE_MUE_DURATION_SECONDS` | `900` | Broker run length; attach takes 1–2.5 min. |
| `OCUDU_NATIVE_SIONNA_AWGN_SNR_DB` | gate default (40) | Noise floor relative to a 0 dB tap: the link-budget knob. |
| `OCUDU_NATIVE_RESCUE_WEB_PORT` / `OCUDU_NATIVE_WEB_PORT` | `8080` / `8081` | Rescue dashboard / the shared radio UI it links. |
| `RESCUE_PYTHON` | `~/venvs/rescue/bin/rescue-python` | The external interpreter (wrapper written by `setup-external.sh`). |

In the GB10 container (`~/ocudu-setup`, not in this repository) the gate is
started through an isolated wrapper that sources the native workspace's
environment, sets `RESCUE_PYTHON=/home/dev/venvs/rescue/bin/rescue-python`,
the 3.0e4 UL calibration and the Isaac world:

```
sudo env RESCUE_ROBOTS=1 bash /home/dev/ocudu-setup/run-isolated-rescue.sh   # 1 robot, 2-UE gate
sudo env RESCUE_ROBOTS=3 bash /home/dev/ocudu-setup/run-isolated-rescue.sh   # 3 robots, 4-UE gate
# knobs: RESCUE_SITE, RESCUE_WORLD (isaac|kinematic), RESCUE_EPISODES, RESCUE_TIME_LIMIT,
#        RESCUE_DURATION (broker s), RESCUE_AWGN_SNR_DB, RESCUE_TX_POWER_UL
```

Dashboard on `http://127.0.0.1:8080` while it runs (the gate refuses to start
when the port is taken). Loopback with the Isaac world and the dashboard:

```
PY=~/venvs/rescue/bin/rescue-python
$PY examples/multi_robot_rescue/run_local.py --out /tmp/rescue-run --world isaac \
    --network blackout --blackout "robot_2:40-60" --web-status-dir /tmp/rescue-web &
$PY examples/multi_robot_rescue/web/server.py --port 8080 --run-dir /tmp/rescue-web
```

The gate's own verdict (`attach-summary.json`) still judges attach, PCI and
transport. The rescue files land in `<log dir>/rescue/`: `links/<robot>/`
(one UE link per robot for the whole gate) and `episodes/eNNN-live/`
(`run.json`, `sim/`, `robots/`, `server/`, `summary.json`).

An episode succeeds when every survivor has been **reported centrally** (the
referee matches reports within 2 m) and the central map knows ≥ 80 % of the
truly free floor (`--coverage-target`); a timeout is a failure.

## Network isolation

- `robot.py` opens only unix sockets; `mission_server.py` only its UDP socket
  and reads nothing from the world or the positions feed
  (`tests/test_rescue_isolation.py` checks the sources).
- In the live gate each `ue_link.py` runs inside its UE's network namespace,
  whose only interface besides `lo` is srsUE's `tun_srsue`; it binds the TUN
  address, so its datagrams to 10.45.1.1 can only leave through the UE.
- The sensor frames and the robot ↔ UE-link bus are unix sockets on the robot
  side and are not radio-transported by design (on a real robot they are
  on-board buses). The positions feed carries ground truth to the bridge and
  the dashboard only.

## Dashboard

`web/server.py` serves one page:

- **Top-down map** (left, always shown): the referee's 2-D truth — walls,
  survivors, robots ringed by link state, trails.
- **Robot view** (right), switched by buttons: one forward camera per robot
  (link state, autonomy mode and backlog overlaid) and *Isaac scene*, an
  overview camera above the building. Rendered by Isaac Sim (`sim.py
  --frames-dir`, 5 Hz, JPEG; walls coloured by material, survivors orange, a
  coloured beacon above each robot that the LiDAR does not see), warmed up
  before the episode clock starts. The kinematic world has no cameras.
  Measured on the GB10 (three robots, four cameras): warm-up 0.9–1.6 s,
  render + encode p50 33 ms per frame set, world at RTF 1.000.
- **Central knowledge**: survivors reported, survivors confirmed on the
  robots, central map coverage, the fused map with last known poses (age,
  uncertainty ring when offline), survivor reports and task targets.
- **Robots and links**: per robot link state, time in state, last reply,
  sRTT, backlog, autonomy mode (`OFFLINE · LOCAL AUTONOMY` badge), goal,
  central task, confirmations, the server's view of it and its pose age.

The header shows the network mode, world, mission, time and a link to the
gate's shared radio UI (RF, HARQ/BLER, GPU panels).

## Known issues (open, 2026-10-15)

Measured in live runs on the GB10 (`$OCUDU_NATIVE_ROOT/results/logs/ocudu-multi-ue/<run>/`);
details and the investigation are in the milestone document.

1. **After a while every robot is OFFLINE and the central side stops
   updating.** Run `20261015T124312Z` (3 robots, Isaac, entrance site): the
   app links went OFFLINE at 235.6 s (robot_2), 284.5 s (robot_1) and 299 s
   (robot_3) after the episode start and never came back; from then on the
   server received nothing, so the central map, poses and survivor count on
   the dashboard froze while the robots kept exploring on their own. Cause on
   the radio side: each moving srsUE lost sync — `SYNC: detected
   out-of-sync`, then `PBCH-MIB: NR SFN (x) does not match current SFN (y)`
   with a fixed frame offset (26, 6 and 54 frames for ue1, ue2, ue0) — got an
   RRC Release, and srsUE `eea87b1d` never re-attaches. Recorded as a limit
   of this srsUE; no UE restart is attempted. The sync loss is not explained
   by channel quality or timing (CQI 15 and a stable strongest-path delay
   right before it, run `20261015T112725Z`) and occurs only on moving UEs;
   frequent channel-history resets on moving links are the open hypothesis.
   Each srsUE internal log grows to 1.4–3.5 GB per run while out of sync.
2. **After a while robots re-enter rooms they already explored and circle.**
   Same run: robot_1 explored N1 at 14–109 s and went back at 218 s for
   155 s; robot_2 crossed the N3 door four times in 281–377 s; robot_3 entered
   S1 three times. Causes visible in the logs and maps: (a) a robot plans on
   its own map only — the server sends targets, never the fused map — so it
   does not know what the others (or it, before a reset) covered, and when
   OFFLINE it has no central task at all; (b) the Isaac LiDAR map has
   scattered occupied cells inside explored rooms, which split free space and
   create small frontiers that pull the robot back; (c) a refused or reached
   goal is blacklisted for 30 s only, after which the same frontier can win
   again. Not fixed yet.

## Limits

See the milestone document for the full list. In short: the live gate has not
been run with this demo yet; both worlds give exact odometry (no SLAM drift)
and the kinematic world has no dynamics; survivor perception is a mock; the
central planner is rule-based (no large model); one gNB, single-port UEs, no
MU-MIMO claim. The live gate runs as root: pass
`RESCUE_PYTHON=/home/dev/venvs/rescue/bin/rescue-python` explicitly.
