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

Set `OCUDU_NATIVE_ROOT` to a pre-provisioned workspace containing the pinned
OCUDU gNB, srsUE, Open5GS, MongoDB, and user-space runtime libraries described
by `native-workspace.lock.json`. The default is
`/home/ubuntu/ocudu-native-workspace`.

The committed bootstrap command is currently verification-only; it does not
download or build a missing workspace:

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
export CUDACXX=/opt/conda/envs/cuda128/bin/nvcc
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

## Common blockers

- `unshare: ... Operation not permitted`: the containing host, VM, LXC, or
  command sandbox disables unprivileged user namespaces.
- `/dev/net/tun is absent`: expose the TUN device to the environment running
  the gate.
- `CUDA hardware probe failed`: check the NVIDIA device/driver visibility and
  `OCUDU_NATIVE_GPU_DEVICE`.
- `workspace lock` or revision mismatch: use the pinned native workspace; the
  gate fails closed instead of silently using different RAN/core binaries.
