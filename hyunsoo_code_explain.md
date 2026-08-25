# OCUDU gNB–UE, Sionna RT, and Web UI: Execution and Code Guide

This document provides the final commands and explains the components used to run the following native 1×1 setup from the current repository.

```text
                           Sionna RT
                               │ Channel profile updates
                               ▼
Open5GS 5GC ── OCUDU gNB ⇄ CUDA Channel Broker ⇄ srsUE
                               │
                               ├── Broker telemetry
                               └── Web UI (read-only)
```

This execution path does not use Docker. It creates isolated user, network, and mount namespaces with ordinary user permissions, then runs the gNB, UE, 5GC, and channel emulator inside them.

## 1. Final execution command

Paste the following block into a terminal.

```bash
cd /home/ubuntu/OCUDU/ocudu-gpu-channel

export OCUDU_NATIVE_ROOT=/home/ubuntu/ocudu-native-workspace
export CUDACXX=/opt/conda/envs/torch/bin/nvcc
export OCUDU_NATIVE_GPU_DEVICE=0
export OCUDU_NATIVE_WEB_PORT=8080

./scripts/native/run-ocudu-sionna-1x1.sh
```

The following messages appear when the complete stack is ready.

```text
event=native_sionna_1x1_live_ready
Web UI: http://127.0.0.1:8080
The live demo keeps running until Ctrl-C.
```

Web UI address:

```text
http://127.0.0.1:8080
```

Press `Ctrl+C` once in the execution terminal to stop the run. The launcher shuts down the Web UI, Sionna, GPU Channel Broker, srsUE, OCUDU gNB, Open5GS, and MongoDB together, then removes the temporary network and mount state.

## 2. Accessing the Web UI on a remote server

If the application is running on a remote GPU server, open an SSH port-forwarding session from the local computer.

```bash
ssh -L 8080:127.0.0.1:8080 ubuntu@GPU_SERVER_IP
```

Then open the following address in a local browser.

```text
http://127.0.0.1:8080
```

For security, the Web UI server binds only to the server's loopback interface and is not exposed directly on a remote interface.

## 3. Environment variables

| Environment variable | Purpose |
|---|---|
| `OCUDU_NATIVE_ROOT` | Native workspace containing the pinned OCUDU, srsUE, Open5GS, and MongoDB binaries, as well as runtime results |
| `CUDACXX` | `nvcc` executable used to compile the CUDA code |
| `OCUDU_NATIVE_GPU_DEVICE` | Physical GPU index used by the CUDA Channel Broker and Sionna |
| `OCUDU_NATIVE_WEB_PORT` | Web UI HTTP port; the default is `8080` |

Set an execution duration in seconds when automatic termination is required.

```bash
export OCUDU_NATIVE_SIONNA_DURATION_SECONDS=150
./scripts/native/run-ocudu-sionna-1x1.sh
```

If this variable is unset or set to `0`, the stack continues running until `Ctrl+C` is pressed.

## 4. Launcher responsibilities

### `run-ocudu-sionna-1x1.sh`

This is the top-level entry point for Sionna mode. It sets `OCUDU_NATIVE_CHANNEL_MODE=sionna` and invokes the shared native 1×1 launcher.

### `run-ocudu-legacy-1x1.sh`

This script is the supervisor for the entire run. Its main responsibilities are:

1. Verify the pinned revisions and file hashes in the native workspace.
2. Generate per-run gNB, UE, Open5GS, subscriber, and Broker configurations.
3. Build the current `ocudu-gpu-channel` checkout with CUDA enabled.
4. Require CTest and the CUDA hardware probe to pass before starting the live radio stack.
5. Start and monitor the isolated namespace runtime and Web UI.
6. Print `live_ready` only after RRC connection, PDU session establishment, ping, Sionna updates, and telemetry reception all succeed.
7. Clean up every child process after `Ctrl+C`, `TERM`, an error, or normal termination.

### `run-ocudu-legacy-1x1-inner.sh`

This script manages the actual processes inside the isolated user, network, and mount namespaces.

- Creates `ogstun` and the UE network namespace.
- Starts MongoDB and the Open5GS 5GC.
- Starts the CUDA Channel Broker.
- Starts the Sionna RT bridge.
- Starts the OCUDU gNB and srsUE.
- Stops each component by its independent process group during cleanup.
- Removes the temporary network namespace, TUN device, and mount state.

## 5. Runtime components

### OCUDU gNB

The OCUDU gNB acts as the 5G base station. It exchanges ZMQ-based `cf32` IQ samples with the CUDA Channel Broker and connects to the Open5GS AMF to handle the UE's RRC and NAS procedures.

### srsUE

srsUE acts as the software-defined UE. It exchanges IQ samples with the gNB through the Broker and performs the following operations:

- Cell search and synchronization
- RRC connection
- 5GC registration
- PDU session establishment
- Data communication through `tun_srsue`

During a successful run, the UE sends ping packets to `10.45.1.1` to verify the data path.

### Open5GS and MongoDB

Open5GS provides the 5G Core functions, including the AMF, SMF, and UPF. MongoDB stores subscriber information. At startup, the launcher registers the subscriber and configures the gNB and UE so they can connect to the 5GC.

### CUDA Channel Broker

The CUDA Channel Broker is a real-time channel emulator placed in the ZMQ IQ path between the gNB and UE.

```text
gNB TX ──▶ Broker ──▶ UE RX
gNB RX ◀── Broker ◀── UE TX
```

The Broker can apply the following channel effects on the GPU:

- Path loss and scalar gain
- Phase
- Delay and multipath taps
- Carrier frequency offset (CFO)
- Additive white Gaussian noise (AWGN)
- Doppler and Rician components
- Superposition of multiple input signals

The Broker publishes telemetry containing each IQ slot's processing time, deadline status, backend application state, and per-channel statistics.

### Sionna RT bridge

The Sionna RT bridge calculates the scene, transmitter and receiver positions, and propagation paths. It converts the calculated rays into tap-based channel profiles understood by the Broker, then atomically updates those profiles through the control endpoint.

The default update rate is 2 Hz. It can be changed with `OCUDU_NATIVE_SIONNA_UPDATE_HZ`.

### Web UI

The Web UI is a read-only observation interface. It does not control the Broker directly. It displays data from two inputs:

- Sionna JSONL status feed
- Broker telemetry feed

The main panels show:

- gNB and UE positions and mobility state
- Sionna iterations and channel generation time
- Control acknowledgements and backend application state
- GPU, CPU, RAM, VRAM, and PCIe status
- IQ slot processing time and the 1 ms deadline
- Per-link path power
- Per-link impulse responses and tap details

The impulse-response graph no longer forces the X-axis to start at `0 samples`. It calculates the axis range and a readable tick interval from the earliest and latest taps. This makes efficient use of the graph width even when all taps are concentrated in a narrow delay range.

For example, when the taps occupy `8.188–9.063 samples`, the axis is approximately:

```text
8.0 samples ───────────────────────── 9.2 samples
```

## 6. Live-ready criteria

The launcher prints `native_sionna_1x1_live_ready` only after all of the following conditions are satisfied:

1. The OCUDU gNB starts successfully.
2. srsUE reaches the `RRC Connected` state.
3. `PDU Session Establishment successful` is observed.
4. A ping to `10.45.1.1` succeeds from the UE namespace.
5. Sionna updates both the downlink and uplink channel profiles.
6. The Broker applies the profiles to the CUDA backend.
7. The Web UI receives both the Sionna and Broker telemetry feeds.

## 7. `Ctrl+C` shutdown and lock handling

The launcher uses `flock` to prevent two native 1×1 runs from starting simultaneously.

Previously, the lock file descriptor could be inherited by child processes such as the gNB, UE, and Open5GS. In addition, `unshare --kill-child` could terminate the inner runner immediately, preventing its cleanup trap from running. The terminal could appear to have stopped while orphaned processes continued holding the lock, causing the following error on the next run:

```text
error: another native 1x1 run is active
```

The current implementation prevents this problem as follows:

- Only the top-level supervisor retains the lock descriptor.
- The lock descriptor is not passed to the long-running namespace runtime or Web UI.
- During shutdown, `TERM` is sent to the inner runner before stopping the `unshare` supervisor.
- The inner runner's cleanup trap stops every process group in order.
- The top-level supervisor waits for cleanup to finish before exiting.

The expected state after a normal `Ctrl+C` shutdown is:

```text
remaining_processes=0
native_lock=free
web_port_8080=closed
```

## 8. Result files

Sionna runtime logs:

```text
/home/ubuntu/ocudu-native-workspace/results/logs/ocudu-sionna-1x1/<UTC timestamp>/
```

Sionna runtime reports:

```text
/home/ubuntu/ocudu-native-workspace/results/reports/ocudu-sionna-1x1/<UTC timestamp>/
```

Important result files:

| File | Contents |
|---|---|
| `live-ready.json` | RRC, PDU session, ping, and live-ready status |
| `web-ui-status.json` | Sionna and Broker feed status observed by the Web UI |
| `attach-summary.json` | Connection results and Broker error counters at shutdown |
| `source-evidence.json` | SHA-256 evidence for the commits, binaries, and configuration files used in the run |
| `sionna-status.jsonl` | Time-series Sionna computation and channel state |

## 9. Prerequisite checks

The host must provide unprivileged user namespaces and a TUN device.

```bash
unshare --user --map-root-user --net --mount --fork /bin/true
test -c /dev/net/tun
```

Both commands must succeed. The CUDA environment and native workspace can be verified with:

```bash
cd /home/ubuntu/OCUDU/ocudu-gpu-channel

export OCUDU_NATIVE_ROOT=/home/ubuntu/ocudu-native-workspace

./scripts/native/bootstrap-workspace.sh \
  --verify-only \
  --root "$OCUDU_NATIVE_ROOT"
```

Expected validation messages:

```text
lock_validation=ok debs=89 archives=3 git_sources=7
claim_boundary=hermetic or arbitrary-clean-host offline build
```

These messages indicate that the pinned native workspace and its dependencies passed validation. They do not indicate that the gNB–UE connection is complete. Use `event=native_sionna_1x1_live_ready` to determine whether the live connection is ready.

## 10. Troubleshooting

### `another native 1x1 run is active`

With the current implementation, a normal `Ctrl+C` shutdown releases the lock and stops the child processes. If the terminal or launcher was forcibly terminated with `SIGKILL`, check whether a process is still using the launcher file.

```bash
fuser scripts/native/run-ocudu-legacy-1x1.sh 2>/dev/null || true
flock -n scripts/native/run-ocudu-legacy-1x1.sh -c 'echo native_lock=free'
```

### The Web UI is not accessible

- Confirm that the launcher printed `native_sionna_1x1_live_ready`.
- Use `http://127.0.0.1:8080` when browsing on the server itself.
- Use SSH `-L` port forwarding from a remote computer.
- Confirm that `OCUDU_NATIVE_WEB_PORT` matches the forwarded SSH port.

### `CUDA hardware probe failed`

```bash
nvidia-smi
/opt/conda/envs/torch/bin/nvcc --version
```

Change `OCUDU_NATIVE_GPU_DEVICE` if the required GPU has a different index.

### `/dev/net/tun is absent`

Expose `/dev/net/tun` to the execution environment through the host, VM, or LXC configuration. The UE data interface cannot be created without a TUN device.

### `unshare: Operation not permitted`

The host or outer container is blocking unprivileged user namespaces. Update the outer host security policy to permit user, network, and mount namespaces.
