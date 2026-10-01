# Rootless native gNB–UE live attach

This directory contains the Docker-free 1×1 live gate:

```text
OCUDU gNB (ZMQ) <-> current ocudu-gpu-channel broker <-> srsUE
                         |
                     Open5GS 5GC
```

It is intentionally a single-gNB, single-UE path. No multi-antenna engine is
required or enabled by this harness.

## Why it does not need host-wide privileges

The runner starts with the invoking user's normal permissions and creates a
user, network, and mount namespace with:

```bash
unshare --user --map-root-user --net --mount
```

`CAP_NET_ADMIN` and `CAP_SYS_ADMIN` exist only inside that disposable user
namespace. The runner creates `ogstun` and the nested `ue1` network namespace
there, then removes them during cleanup. It does not invoke `sudo`, Docker, or
modify the host network namespace.

The host must still permit unprivileged user namespaces and expose
`/dev/net/tun` to the user. Test the first requirement with:

```bash
unshare --user --map-root-user --net --mount --fork /bin/true
test -c /dev/net/tun
```

## Required native workspace

Set `OCUDU_NATIVE_ROOT` to the dedicated workspace containing the pinned OCUDU
gNB, srsUE, Open5GS, MongoDB, and user-space runtime libraries described by
`native-workspace.lock.json`. The default is
`/home/ubuntu/ocudu-native-workspace`.

Provision it without sudo or Docker from the repository root:

```bash
export OCUDU_NATIVE_ROOT="$HOME/ocudu-native-workspace"
./scripts/native/bootstrap-workspace.sh \
  --root "$OCUDU_NATIVE_ROOT" --jobs "$(nproc)"
```

The bootstrap downloads hash-locked inputs, extracts the user-space dependency
overlay, checks out the exact audited revisions, and builds every binary and
Open5GS module used by the live gate. To validate an existing workspace without
downloading or rebuilding it:

```bash
export OCUDU_NATIVE_ROOT="$HOME/ocudu-native-workspace"
./scripts/native/bootstrap-workspace.sh \
  --verify-only --root "$OCUDU_NATIVE_ROOT"
```

The relevant pinned binaries are:

- `builds/ocudu-zmq-release/apps/gnb/gnb`
- `builds/srsran4g-zmq-release/srsue/src/srsue`
- `builds/open5gs-v2.7.6/tests/app/5gc`
- `install/mongodb-6.0.29/bin/mongod`

The broker is not taken from a stale workspace build. The live gate configures
and builds the current checkout into
`builds/ocudu-gpu-channel-cuda-release`, probes that exact binary, and passes
the same path to the runtime namespace.

## Run the 1×1 attach gate

From the repository root:

```bash
export OCUDU_NATIVE_ROOT="$HOME/ocudu-native-workspace"
# This host's installed CUDA compiler. Override it if CUDA is elsewhere.
export CUDACXX="$(command -v nvcc)"
export OCUDU_NATIVE_GPU_DEVICE=0

./scripts/native/run-ocudu-legacy-1x1.sh
```

The gate performs these checks in order:

1. Verifies pinned source revisions, cached inputs, native binaries, ZMQ, SCTP,
   `/dev/net/tun`, and rootless namespace support.
2. Renders loopback-only gNB, broker, Open5GS, subscriber, and srsUE configs.
3. Builds and tests the current broker with CUDA enabled.
4. Probes CUDA and the new broker inside an isolated user/network/mount
   namespace.
5. Starts MongoDB, Open5GS, the broker, OCUDU gNB, and srsUE.
6. Requires RRC connection, PDU-session establishment, and three successful
   pings from `tun_srsue` to `10.45.1.1`.

A successful run ends with:

```text
event=native_legacy_1x1_attach_gate result=pass ...
```

Run evidence is stored under:

```text
$OCUDU_NATIVE_ROOT/results/logs/ocudu-interop/<UTC timestamp>/
$OCUDU_NATIVE_ROOT/results/reports/ocudu-interop/<UTC timestamp>/
```

The report includes the exact source manifest, binary/configuration SHA-256
hashes, Broker counters, and attach summary. Any process or namespace created
by the gate is terminated on normal exit, failure, or Ctrl-C.

## Run gNB–UE with Sionna RT and the Web UI

The Sionna path uses the same Docker-free 1×1 gNB–UE stack. It adds a
low-rate Sionna RT controller and the read-only Web UI; it does not enable a
multi-antenna topology. Only one terminal is required:

```bash
export OCUDU_NATIVE_ROOT="$HOME/ocudu-native-workspace"
export CUDACXX="$(command -v nvcc)"
export OCUDU_NATIVE_GPU_DEVICE=0

./scripts/native/run-ocudu-sionna-1x1.sh
```

The launcher discovers the locally installed OptiX library and uses
`../venvs/sionna/bin/python` by default. Override the Python executable with
`OCUDU_NATIVE_SIONNA_PYTHON` when needed. It prints the Web UI URL after the
HTTP server starts and prints `event=native_sionna_1x1_live_ready` only after
all of these conditions hold:

1. The OCUDU gNB and srsUE establish RRC and a PDU session.
2. The UE namespace successfully pings `10.45.1.1` through `tun_srsue`.
3. Sionna RT atomically updates both directed 1×1 channel profiles.
4. The Web UI receives both the Sionna JSONL feed and Broker telemetry.

Open <http://127.0.0.1:8080> while the command is running. The default live
run continues until Ctrl-C. For an automatically terminating evidence run,
set a duration in seconds before starting it:

```bash
export OCUDU_NATIVE_SIONNA_DURATION_SECONDS=150
./scripts/native/run-ocudu-sionna-1x1.sh
```

Optional settings are `OCUDU_NATIVE_WEB_PORT` (default `8080`),
`OCUDU_NATIVE_SIONNA_UPDATE_HZ` (default `10`), and
`OCUDU_NATIVE_SIONNA_READY_SECONDS` (default `120`). The Web server is
restricted to loopback.

Node positions can come from a live publisher instead of the scenario's
scripted routes (the robot arena of `ROBOT_FIGHT_MILESTONES.md`):
`OCUDU_NATIVE_SIONNA_POSITION_ENDPOINT` is passed to the bridge as
`--position-endpoint`, with the optional `OCUDU_NATIVE_SIONNA_POSITION_OFFSET`
(`x,y,z`, arena origin in scene metres) and
`OCUDU_NATIVE_SIONNA_POSITION_TIMEOUT_S` (default `1`). The bridge runs inside
the gate's `unshare --net` namespace, so a `tcp://127.0.0.1` publisher on the
host is unreachable from it: the endpoint must be an absolute `ipc://` socket
that the publisher **binds** on the shared filesystem, for example
`ipc:///workspace/ocudu-spark/run/arena/positions.sock`, and the bridge
connects to it (the same rule as the control and telemetry sockets). The
message is one JSON frame per update,
`{"event":"positions","t_unix_ms":…,"frame":"arena","nodes":{"ue0":{"position_m":[x,y,z],"velocity_mps":[vx,vy,vz]}}}`;
nodes it omits keep their scripted route, unknown node ids are ignored with
one warning, and after the timeout the last positions are kept and flagged
`stale` in the `sionna_rt_update` status record (`position_source`,
`position_status`). The same variables apply to `run-ocudu-sionna-multi-ue.sh`.

The Sionna multi-UE gate (`run-ocudu-sionna-multi-ue.sh`, the R4 base of
`ROBOT_FIGHT_MILESTONES.md`) has these further knobs; every default keeps the
run the gate always did:

| Variable | Default | Effect |
| --- | --- | --- |
| `OCUDU_NATIVE_SIONNA_AWGN_SNR_DB` | `40` (`off` = none) | Absolute receiver noise floor on every node (`rx_model`, an `awgn` step with `noise_power`), sized so a unit-gain link sees this SNR: `noise = tx_power / 10^(snr/10)` with the transmit levels measured on the wire (`render-sionna-multi-ue-configs.py` `TX_POWER_DL`/`TX_POWER_UL`, `wire-capture-power.py`). A `snr_db` step would size its noise against the *faded* signal and give a shadowed UE the same SNR as one in line of sight; the absolute floor is what makes Sionna's path loss reach the decoder. On the ring scene 40 dB reads as 34 dB (LOS) / ~16 dB (pillar shadow) at srsUE; 26.6 and 34.6 break srsUE's initial access and sync in the shadow. Sionna mode only. |
| `OCUDU_NATIVE_SIONNA_TX_POWER_DL` / `_UL` | renderer constants | Override the measured transmit levels (mean `\|x\|^2` of active samples). |
| `OCUDU_NATIVE_MUE_DURATION_SECONDS` | `240` (min 160) | Broker `--duration`; the attach window is the first 150 s. |
| `OCUDU_NATIVE_MUE_STRICT_REALTIME` | `0` | Pass `--strict-realtime` to the broker (it then exits 1 if any starvation/overflow/gap counter is non-zero, which fails the gate). |
| `OCUDU_NATIVE_MUE_UE_EXEC` | unset | A `bash -c` template started inside each UE's network namespace right after that UE's ping passes (the tun and its address exist only then). Placeholders: `{ue_id}` (ue0…), `{ue_ip}` (10.45.1.2…), `{ue_index}`, `{ue_netns}`, `{ue_gateway}`, `{log_dir}`, `{run_dir}`, `{config_dir}`. Only the network namespace is entered, so the filesystem (ipc sockets, logs) is shared with the stack. Logged to `ue-exec-<ue>.log`, stopped first at teardown. |
| `OCUDU_NATIVE_MUE_ROOT_EXEC` | unset | The same for one command in the stack's own namespace (where `ogstun` 10.45.1.1, the broker and the bridge live), started as soon as `ogstun` exists — the place for a robot brain the UEs reach at `{ue_gateway}`. Placeholders: `{ue_ids}`, `{ue_ips}`, `{ue_gateway}`, `{log_dir}`, `{run_dir}`, `{config_dir}`. Logged to `root-exec.log`. |
| `OCUDU_NATIVE_MUE_WIRE_CAPTURE_SAMPLES` / `_SKIP_SECONDS` | `0` / `60` | Broker wire capture per port and direction into `<log_dir>/wire-capture`; `wire-capture-power.py` reads it. |
| `OCUDU_NATIVE_MUE_PIN_UES` | `1` | With a platform profile, the srsUEs are pinned to the profile's UE cores like the OAI gates' nrUE; `0` leaves them unpinned. |

The gate also applies the platform CPU placement of `platform-profiles.json`
(`OCUDU_NATIVE_PLATFORM=none` turns it off), writes every srsUE's per-second
metrics to `srsue-metrics-<ue>.csv` (with `srsue-<ue>.start_unix_ms` for the
wall clock), records what it applied in `run-parameters.json`, and its
`attach-summary.json` carries the control-plane counters, the last broker
heartbeat per device and the Sionna feed status next to the verdict.
`analyze-sionna-multi-ue-run.py --log-dir …` joins the UE metrics with the
bridge's positions and per-link gains and reports the link quality in line of
sight against the shadow. A remote browser can use SSH port forwarding:

```bash
ssh -L 8080:127.0.0.1:8080 YOUR_USER@GPU_HOST
```

### Keeping the RAN KPI panel populated

`OCUDU_NATIVE_GNB_METRICS=1` turns on the per-UE scheduler KPI panel. Two
further switches decide whether that panel has anything to show, and both are
off by default so every gate renders and runs exactly as before:

| Variable | Default | Effect |
| --- | --- | --- |
| `OCUDU_NATIVE_UE_INACTIVITY_SECONDS` | unset (gNB default, 120 s) | Renders `cu_cp.inactivity_timer`, accepted range 1-7200. Without it the gNB releases an idle UE about two minutes after the acceptance ping and the panel correctly reports no connected UEs. |
| `OCUDU_NATIVE_UE_KEEPALIVE_SECONDS` | `0` (off) | Ping interval in the UE namespace, accepted range 0.2-60 s, fractional. Started only on an unbounded run and only after the acceptance verdict is written, logging to `ue-keepalive.log`. |

Set the keepalive interval well under the scheduler's report period
(`metrics.periodicity.du_report_period`, 1 s as rendered). The KPIs are sums
over one report period, so an interval at or above that period leaves the
periods in between reporting zero throughput, zero HARQ counts and no SINR --
truthfully, since nothing was transmitted in them. `0.2` puts five packets in
every report and keeps the row continuously populated:

```bash
OCUDU_NATIVE_GNB_METRICS=1 \
OCUDU_NATIVE_UE_INACTIVITY_SECONDS=7200 \
OCUDU_NATIVE_UE_KEEPALIVE_SECONDS=0.2 \
./scripts/native/run-ocudu-sionna-rank1.sh
```

The CQI and DL RI columns stay empty regardless: this configuration runs with
`csi_rs_enabled: false`, so the scheduler reports `cqi = -1` ("no CSI report")
for the whole run.

Sionna mode stores logs and reports separately from the legacy gate:

```text
$OCUDU_NATIVE_ROOT/results/logs/ocudu-sionna-1x1/<UTC timestamp>/
$OCUDU_NATIVE_ROOT/results/reports/ocudu-sionna-1x1/<UTC timestamp>/
```

The runtime uses Unix-domain ZeroMQ endpoints under the native workspace for
Sionna control and Web UI telemetry. They cross the disposable mount/network
namespace through the shared filesystem without exposing a host TCP control
port or requiring host networking privileges.

## Run scenario-sized rank-1 MISO/SIMO with Sionna and the Web UI

The rank-1 launcher reads the gNB transmit and receive port counts from the
Sionna scenario instead of carrying a separate 2x1 or 4x1 shell setting. The
default scenario resolves to 4 gNB TX ports, 4 gNB RX ports, and a one-port UE
(4x1 DL MISO and 1x4 UL SIMO):

```bash
export OCUDU_NATIVE_ROOT="$HOME/ocudu-native-workspace"
export CUDACXX="$(command -v nvcc)"
export OCUDU_NATIVE_GPU_DEVICE=0
export OCUDU_NATIVE_SIONNA_PYTHON="$HOME/venvs/sionna/bin/python"

./scripts/native/run-ocudu-sionna-rank1.sh
```

To change the live dimensions, point the launcher at an absolute scenario
path and edit `nodes.gnb0.tx_array` / `nodes.gnb0.rx_array`. The native OCUDU
gate accepts 1, 2, or 4 ports in each direction; the srsUE arrays must remain
1x1. The scenario links must remain the single `gnb0 -> ue0` downlink and
`ue0 -> gnb0` uplink:

```bash
export OCUDU_NATIVE_SIONNA_SCENARIO=/absolute/path/to/scenario.json
./scripts/native/run-ocudu-sionna-rank1.sh
```

The renderer derives `nof_antennas_dl`, `nof_antennas_ul`, every ZMQ port,
Broker radio-node ordering, and both control link ids from that scenario. It
does not declare `fixed_mimo`; Sionna supplies a complete matrix profile for
each direction before the gNB and srsUE start. Readiness is reported as
`event=native_sionna_rank1_live_ready`, and run artifacts are stored under
`results/{logs,reports}/ocudu-sionna-rank1/`.

## Common blockers

- `unshare: ... Operation not permitted`: the containing host, VM, LXC, or
  command sandbox disables unprivileged user namespaces.
- `/dev/net/tun is absent`: expose the TUN device to the environment running
  the gate.
- `CUDA hardware probe failed`: check the NVIDIA device/driver visibility and
  `OCUDU_NATIVE_GPU_DEVICE`.
- `workspace lock` or revision mismatch: use the pinned native workspace; the
  gate fails closed instead of silently using different RAN/core binaries.

## Shutdown and troubleshooting the Sionna 1×1 run

Press Ctrl-C once in the launch terminal and allow cleanup to finish. The
supervisor retains the run lock, closes that descriptor in child processes,
and signals the inner runner before stopping its namespace supervisor. The
inner runner stops its process groups and removes its temporary namespace
and TUN state. Normal shutdown releases the lock and closes the Web port.

If `another native 1x1 run is active` appears after an interrupted run, inspect
the lock before starting another instance; a remaining process may still own it:

```bash
fuser scripts/native/run-ocudu-legacy-1x1.sh 2>/dev/null || true
flock -n scripts/native/run-ocudu-legacy-1x1.sh -c 'echo native_lock=free'
```

For an inaccessible dashboard, check the printed URL, the configured
`OCUDU_NATIVE_WEB_PORT`, and the SSH forwarding port. An HTTP response alone
is not proof of radio attachment: check `native_sionna_1x1_live_ready` and the
run's RRC, PDU and ping evidence. For a CUDA probe failure, inspect
`nvidia-smi`, `"$CUDACXX" --version`, and the selected GPU index. A missing
TUN device or blocked `unshare` requires a suitable host/container environment.

The report directory contains `live-ready.json`, `web-ui-status.json`,
`attach-summary.json`, and `source-evidence.json`; the log directory contains
`sionna-status.jsonl`. Workspace verification checks pinned dependencies and
binaries, not radio connectivity. See the
[current Sionna guide](../../docs/sionna-integration.md) for matrix updates,
telemetry freshness, multi-gNB metrics and the separate UE recovery results.

## Run the two-cell, two-broker robot-fight gate (R5)

`run-ocudu-robot-fight.sh` is the scheduling battle of
`ROBOT_FIGHT_MILESTONES.md` R5: cell a (`gnb0` PCI 1 <-> `ue0`) and cell b
(`gnb1` PCI 2 <-> `ue1`) are served by **two separate broker processes** on
one GPU, each driven by its own Sionna bridge from one scene
(`examples/sionna/robot-ring-fight.json`, `gnb1` on `gnb0`'s mast) and one
live position feed. The physical channel is the same by construction; the
knobs decide how each broker is scheduled while something else shares the
GPU. It keeps the multi-UE gate's hooks (`OCUDU_NATIVE_MUE_UE_EXEC`,
`OCUDU_NATIVE_MUE_ROOT_EXEC`), position-feed and noise-floor variables.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OCUDU_NATIVE_RF_BROKER_A_SCHED` / `_B_SCHED` | `plain` | `plain`: own CUDA context, unpinned, default stream, no realtime class. `protected`: MPS client, `runtime.cuda_stream_priority` (`OCUDU_NATIVE_RF_PROTECTED_STREAM_PRIORITY`, `high`), pinned to the profile's broker cores (`OCUDU_NATIVE_RF_PROTECTED_CPUS`) with the profile's `broker_env`; optionally `chrt -f` `OCUDU_NATIVE_RF_PROTECTED_RT_PRIORITY` (`0` = off, the default: with `OCG_BROKER_SPIN=1` the broker's spinning threads starve each other under FIFO, R5a; needs the rlimit, the inner logs `rt_unavailable` if it could not). |
| `OCUDU_NATIVE_RF_CONTENTION` | `none` | `busy`: `ocudu-gpu-hog` (spin kernels of `OCUDU_NATIVE_RF_HOG_KERNEL_US` µs at duty `OCUDU_NATIVE_RF_HOG_DUTY`, an MPS client when `OCUDU_NATIVE_RF_HOG_MPS=1`, then capped to `OCUDU_NATIVE_RF_HOG_SM_PERCENT` of the SMs via `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` when set). `cudagnb`: both gNBs CUDA-accelerated (`OCUDU_NATIVE_GNB_ACCELERATION`, `all`). `busy+cudagnb`. The two Sionna bridges always share the GPU; they are MPS clients by default (`OCUDU_NATIVE_RF_SIONNA_MPS=0` gives each its own context, which time-slices every broker for the length of each solve). A 2,000 µs hog kernel costs every context outside its own ~2.3 ms per slot whatever the broker does; `OCUDU_NATIVE_RF_HOG_KERNEL_US=200` is the realistic many-small-kernels tenant (CUDA gNB, Sionna), against which the protected broker holds p99 0.86 ms vs 1.87 ms plain (R5a). |
| `OCUDU_NATIVE_RF_HOG_STREAMS` / `_HOG_QUEUE_DEPTH` | `1` / `1` | Kernels the hog keeps queued: S streams × K deep (R5b). Inside the shared MPS context a default-priority kernel from another client lines up behind everything queued, a high-priority stream only behind the running blocks — so the queue, not the kernel length, is what separates a protected broker from a plain one. A broker in its own context pays the time-slice quantum (~2.2 ms/slot) whatever the depth; `200 µs × 2 × 16` is the R5b battle point (protected p99 0.84 ms, plain 2.3 ms, ping 28 vs 65 ms). |
| `OCUDU_NATIVE_RF_CONTENTION_START` | `after-attach` | The hog starts once both UEs have pinged (`immediate`: with the brokers). |
| `OCUDU_NATIVE_RF_MPS` | `auto` | One MPS server for the gate when any process is meant to be a client (`with-cuda-mps.py` re-executes the gate); `on` / `off`. |
| `OCUDU_NATIVE_RF_DURATION_SECONDS` | `300` | Broker `--duration`; attach window 150 s, the rest under contention. |
| `OCUDU_NATIVE_RF_PLAIN_CPUS` | unset | Pin the plain broker too (unpinned by default). |
| `OCUDU_NATIVE_RF_SKIP_CTEST` | `0` | Skip the tree's ctest after the build (repeat runs of one tree). |
| `OCUDU_NATIVE_RF_WEB_UI` | `0` | Start the read-only web UI on cell a's telemetry (with cell a's gNB KPI feed when metrics are on). |
| `OCUDU_NATIVE_GNB_METRICS` | **`1`** (this gate only; every other gate defaults off) | Render both gNBs with `metrics.enable_json` + `remote_control` and start one `gnb-metrics-relay.py` per gNB, exporting `gnb-metrics-{a,b}.sock` in the run dir (`run-parameters.json` → `gnb_metrics`). The Web UI's KPI panel needs `--gnb-metrics-endpoint ws+unix://<that socket>`; `robot-fight-webui-follow.sh` does that for the newest run on 8080 (cell a) / 8081 (cell b). `0` leaves the gNB configs and the process list as before. |
| `OCUDU_NATIVE_GNB_METRICS_PORT_A` / `_B` | `8001` / `8002` | Remote-control ports on the stack loopback; must differ because both gNBs share it. |
| `OCUDU_NATIVE_RF_GNB_CELL_OVERRIDES` | unset | RAN latency knobs applied to BOTH gNBs (R7-prep): `pusch.min_k2=8,pucch.sr_period_ms=40,pucch.min_k1=7,pdsch.max_nof_harq_retxs=1,pusch.max_nof_harq_retxs=1` (any subset; OCUDU defaults 4 / 20 / 4 / 4 / 4). Recorded in `robot-fight-shape.json` → `cell_overrides`. Unset leaves the gNB yaml bit-identical. |

The verdict (`results/reports/ocudu-robot-fight/<ts>/attach-summary.json`)
passes on attach, PCI camping, clean transport counters and broker exit; it
*records* per broker `rx_starvations`, node stalls and `gpu_timings`
percentiles before and under contention, the ping RTT of each UE at attach
and under contention (a 50-packet burst 20 s after the contention starts),
the Sionna feed counters and the hog's rate. Those are the measurement.

## Run the scheduler benchmark (two cells × two UEs, one channel)

`run-ocudu-scheduler-benchmark.sh` compares two OCUDU MAC scheduling
policies under the same radio conditions. Cell a (`gnb0` PCI 1 with `ue0`,
`ue1`) and cell b (`gnb1` PCI 2 with `ue2`, `ue3`) run side by side on two
broker processes. **One** Sionna bridge solves a seeded scenario whose radios
are `gnb0`, `ue0`, `ue1` and sends every batch to both brokers
(`run_bridge.py --fanout-control-endpoint`, cell b under the rename
`gnb0→gnb1, ue0→ue2, ue1→ue3`), so `ue2` carries exactly `ue0`'s channel and
`ue3` exactly `ue1`'s, at the same instant. The cells differ only in
`cell_cfg.scheduler.policy`: `rr` (`rr_sched`, `scheduler_time_rr.cpp`) or
`qos` (`qos_sched`, `scheduler_time_qos.cpp`, OCUDU's default). The policy is
bound at gNB start-up and the remote-control commands cannot change it, which
is why the two policies run in parallel rather than in alternating periods.

**Reproducibility.** `OCUDU_NATIVE_SB_SEED` fixes the UE random-waypoint
routes (`scripts/scheduler_benchmark/make_scenario.py`, kept in line of sight
of the gNB against the scene's obstacle boxes), the PathSolver seed and every
packet schedule. The bridge runs `--timeline grid`: update k is solved at
scenario time k / update_hz, never at "elapsed wall time", and scenario time
0 is held (`--hold-until-file`) until all four UEs have attached. Within a
run both cells receive the same solved batch, so their channels are equal by
construction. Across runs with the same seed (live runs on the GB10,
2026-10-01: ten run pairs, seven seed 1 and two seed 2 runs, 701-1322 shared
grid points per pair) the UE positions and the tap structure (count, delays)
were identical at every grid point, and 89-98% of the batches were
bit-identical (SHA-256 per batch, `--profile-digest`); at the rest the GPU's
floating-point summation order differed, by at most 0.0038 dB in tap gain
and 0.0005 rad in phase.
`scripts/scheduler_benchmark/campaign.py` recomputes this for any set of
runs. The radio stack itself (thread timing, HARQ, when a channel update
lands relative to the IQ stream) is not deterministic; that is what the
per-second pairing and the A/A control below measure.

**Sequence.** Core, two brokers, the bridge (scenario time 0 applied), two
gNBs; the gate then checks each gNB's non-default configuration dump
(`log.config_level: info`) for the requested policy and refuses to continue
on a mismatch (`policy-check.json`). Four srsUEs attach; then at one instant
the seeded UDP traffic (`scripts/scheduler_benchmark/traffic.py`) starts and
the bridge's grid point 0 is released. After `OCUDU_NATIVE_SB_MEASURE_SECONDS`
and a drain, `scripts/scheduler_benchmark/analyze.py` writes
`results/reports/ocudu-scheduler-benchmark/<ts>/benchmark-report.json`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OCUDU_NATIVE_SB_SEED` | `1` | Scenario, solver and traffic seed. |
| `OCUDU_NATIVE_SB_SCHED_A` / `_B` | `rr` / `qos` | Policy of cell a / b. Run each seed twice with the policies swapped to cancel cell bias, and once with the same policy in both cells (A/A) to measure it. |
| `OCUDU_NATIVE_SB_PROFILE` | `mixed` | Traffic profile (`definitions.py`): `mixed` = UE A backlogged download, 5QI 9; UE B on/off video and 100 Hz control, 5QI 7. `full_buffer` = both backlogged, 5QI 9. `light` = Poisson well below capacity. The per-UE 5QI goes into the subscriber CSV's `qci` column. |
| `OCUDU_NATIVE_SB_TRAFFIC_OVERRIDES` | unset | `ue0.dl_bulk.rate_mbps=25,ue1.five_qi.value=9`, … |
| `OCUDU_NATIVE_SB_MEASURE_SECONDS` | `120` | Scenario seconds measured; the report pairs them second by second. |
| `OCUDU_NATIVE_SB_CPUS_*` | profile | On `spark-gb10`: both gNBs share 5-12, the four UEs share 15-19, brokers 13 / 14, everything else 0-4. A gNB must see at least five CPUs: OCUDU sizes its main worker pool as min(needed, CPUs − 3), and with one worker it stalls at MAC cell activation. |
| `OCUDU_NATIVE_SB_WEB_UI` / `_WEB_PORT` | `1` / `8090` | The benchmark UI (`scripts/scheduler_benchmark/server.py --follow-latest`). |

**Metrics.** Every metric is computed per valid second (all four UEs in
service: present in their gNB's scheduler report with DL HARQ not failing
outright, and, when srsUE has written its metrics CSV, camped on its own PCI)
and reported per cell with a paired a − b difference and a batch-means 95%
CI. Application layer (UDP, one host clock): aggregate and per-UE
throughput, Jain's index, mean and p95/p99 one-way delay, PDB violation
rate against the UE's 5QI, drop rate. MAC layer (OCUDU `Slot decisions`
log, `lib/scheduler/log_reference.md`): PRB utilization, PRB shares,
spectral efficiency, DL wait while data is queued (`dl_bo > 0`) as the
starvation measure, and the scheduler's per-slot decision time. GBR
satisfaction is not measured: srsUE has one default non-GBR QoS flow.

**Radio speed.** The report and the UI state each cell's slots per second
(real time is 1000 at 15 kHz SCS). On the GB10 with this placement eight
`mixed` runs (2026-10-01) ran at 393-654 slots/s, varying from run to run
with no other busy process in the host-load samples (cause not identified);
within a run the two cells stayed within 4% of each other, so the comparison
stays paired, but wall-clock capacities and delays scale with this factor
and absolute numbers from different runs are not comparable.

**Cell bias is real; use swap pairs.** In the seed-1 A/A run (`rr` in both
cells, 20261001T083522Z) UE A's DL p99 delay was 244 ms higher in cell a
(95% CI 132-357 ms) and the aggregate DL 0.84 Mb/s lower (CI 0.40-1.28),
with the same policy, channel and traffic. A single run's paired CI therefore
does not separate the policy from the cell; the swap pair does.

**Known failure.** srsUE (srsRAN 4G 23.04 fork) can abort in RLC AM NR
(`rlc_am_nr.cc:1776`, `SO_start > SO_end` on a NACK segment) under the
backlogged 5QI 9 flow; one of eight runs lost UE A of cell a this way after
60 s. The lock-step broker then stalls the whole cell, the analyzer marks
those seconds invalid, and the gate fails on the crash (`per_ue.*.crashed`,
`srsue_alive_at_stop`).

In this container: `sudo env SB_SEED=1 SB_MEASURE=120 bash
/home/dev/ocudu-setup/run-isolated-scheduler-benchmark.sh`, or
`SB_CAMPAIGN="1:rr:qos 1:qos:rr 1:rr:rr"` for a swap pair plus an A/A run;
`scripts/scheduler_benchmark/campaign.py <report dirs>` turns those into
cell-bias-free policy effects.
