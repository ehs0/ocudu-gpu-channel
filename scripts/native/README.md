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
export OCUDU_NATIVE_ROOT=/home/ubuntu/ocudu-native-workspace
./scripts/native/bootstrap-workspace.sh \
  --root "$OCUDU_NATIVE_ROOT" --jobs "$(nproc)"
```

The bootstrap downloads hash-locked inputs, extracts the user-space dependency
overlay, checks out the exact audited revisions, and builds every binary and
Open5GS module used by the live gate. To validate an existing workspace without
downloading or rebuilding it:

```bash
export OCUDU_NATIVE_ROOT=/home/ubuntu/ocudu-native-workspace
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
export OCUDU_NATIVE_ROOT=/home/ubuntu/ocudu-native-workspace
# This host's installed CUDA compiler. Override it if CUDA is elsewhere.
export CUDACXX=/opt/conda/envs/torch/bin/nvcc
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
export OCUDU_NATIVE_ROOT=/home/ubuntu/ocudu-native-workspace
export CUDACXX=/opt/conda/envs/torch/bin/nvcc
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
restricted to loopback. A remote browser can use SSH port forwarding:

```bash
ssh -L 8080:127.0.0.1:8080 ubuntu@GPU_HOST
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
export OCUDU_NATIVE_ROOT=/home/ubuntu/ocudu-native-workspace
export CUDACXX=/opt/conda/envs/torch/bin/nvcc
export OCUDU_NATIVE_GPU_DEVICE=0
export OCUDU_NATIVE_SIONNA_PYTHON=/home/ubuntu/OCUDU/venvs/sionna/bin/python

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
