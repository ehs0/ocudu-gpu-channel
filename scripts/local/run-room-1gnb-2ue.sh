#!/usr/bin/env bash
# Baseline run of the indoor room: one 4-antenna gNB, two srsUE humanoids
# walking the room, Sionna RT driving every channel. Stack comes up in Docker
# through the single-cell mode of the multi-gNB smoke and stays up for the
# Web UI (http://127.0.0.1:8080).
#
#   scripts/local/run-room-1gnb-2ue.sh
#
# Every setting below is the one measured passing 10/10 on 2026-09-29; any
# of them can be overridden from the environment.
#
# - One cell. Two co-channel cells share the PRACH root sequence, so each
#   answers the other's preambles; one cell has nothing to split.
# - No UE launch stagger (the single-cell default of the smoke). Holding ue1
#   back freezes the whole network until it starts, because the broker waits
#   for every incoming lane; ue1 then attempts access in the catch-up burst.
# - Distinct PRACH preamble per UE, set by the smoke. srsUE pins both the
#   preamble and the occasion to 0, so two UEs would otherwise collide on
#   every attempt.
# - One channel scaling for both directions. The channel is what Sionna
#   computed; the direction asymmetry sits in the radios instead -- the gNB's
#   ZMQ device refuses gain above 0 dB -- and is corrected there, with the
#   UE's ZMQ transmit gain (a sample multiplier, not dBm) at 30 rather than
#   the 50 inherited from the B210 fixture.
# - No UE<->UE crosstalk. Band n3 is FDD, so one UE's uplink cannot reach
#   another's downlink demodulator as a waveform (TS 38.101-1); the scenario
#   and its topology carry no such lane.
#
# The native runner is not used: it needs unprivileged user namespaces, which
# Ubuntu 24.04's AppArmor restricts by default.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

export OCUDU_MGNB_CELLS="${OCUDU_MGNB_CELLS:-1}"
export OCUDU_MGNB_CHANNEL_MODE="${OCUDU_MGNB_CHANNEL_MODE:-sionna}"
export OCUDU_MGNB_SIONNA_SCENARIO="${OCUDU_MGNB_SIONNA_SCENARIO:-examples/sionna/room-table-1gnb-2ue.json}"
export OCUDU_MGNB_SIONNA_PYTHON="${OCUDU_MGNB_SIONNA_PYTHON:-${HOME}/OCUDU/venvs/sionna/bin/python}"
export OCUDU_MGNB_SIONNA_UPDATE_HZ="${OCUDU_MGNB_SIONNA_UPDATE_HZ:-10}"
export OCUDU_MGNB_SIONNA_EXTRA_ARGS="${OCUDU_MGNB_SIONNA_EXTRA_ARGS:---gain-offset-db 32 --max-depth 4 --samples-per-src 50000}"
export OCUDU_MGNB_UE_TX_GAIN="${OCUDU_MGNB_UE_TX_GAIN:-30}"
export OCUDU_MGNB_DURATION_SECONDS="${OCUDU_MGNB_DURATION_SECONDS:-120}"
# Keep the stack up for the dashboard, and keep attached UEs attached: the
# gNB's default 120 s inactivity timer releases an idle UE long before that.
export OCUDU_MGNB_HOLD_SECONDS="${OCUDU_MGNB_HOLD_SECONDS:-3600}"
export OCUDU_MGNB_UE_INACTIVITY_SECONDS="${OCUDU_MGNB_UE_INACTIVITY_SECONDS:-3600}"
export OCUDU_MGNB_BUILD_DOCKER="${OCUDU_MGNB_BUILD_DOCKER:-0}"
export OCUDU_MGNB_CUDA_COMPILER="${OCUDU_MGNB_CUDA_COMPILER:-/usr/local/cuda/bin/nvcc}"
export OCUDU_MGNB_OCUDU_ROOT="${OCUDU_MGNB_OCUDU_ROOT:-${HOME}/ocudu-native-workspace/src/ocudu}"

[[ -x "${OCUDU_MGNB_SIONNA_PYTHON}" ]] || {
  printf 'Sionna python is not executable: %s\n' "${OCUDU_MGNB_SIONNA_PYTHON}" >&2
  exit 2
}
[[ -f "${repo_root}/${OCUDU_MGNB_SIONNA_SCENARIO}" || -f "${OCUDU_MGNB_SIONNA_SCENARIO}" ]] || {
  printf 'missing scenario: %s\n' "${OCUDU_MGNB_SIONNA_SCENARIO}" >&2
  exit 2
}

printf 'scenario: %s\n' "${OCUDU_MGNB_SIONNA_SCENARIO}"
printf 'bridge:   %s\n' "${OCUDU_MGNB_SIONNA_EXTRA_ARGS}"
cd "${repo_root}"
exec "${repo_root}/scripts/local/ocudu-gnb-ue-sionna-smoke.sh" "$@"
