#!/usr/bin/env bash
# V2X blind-intersection demo over the live 5G link (native multi-UE gate, Sionna RT).
#
# One OCUDU gNB, two srsUEs, Open5GS, this tree's broker and the Sionna
# bridge (scripts/native/run-ocudu-sionna-multi-ue.sh). ue0 is vehicle A's UE:
# the bridge follows A's position from drive.py's positions feed; ue1 is
# parked in line of sight (the gate needs two UEs). The gate's hooks add the
# application (launch/root_exec.sh, launch/ue_exec.sh): roadside server ->
# UDP over the 5G user plane -> A's UE client -> A's controller -> SUMO ->
# A's position -> Sionna RT -> broker -> the IQ the UE receives.
#
# Run from the repo root, as root (the gate creates network namespaces):
#   bash examples/v2x_intersection/run-ocudu-v2x-intersection.sh
#
# Knobs (defaults):
#   OCUDU_NATIVE_V2X_SITE            blocked | los (blocked): which scenario JSON
#   OCUDU_NATIVE_V2X_MODES           v2x,no-v2x: drive.py modes cycled per episode
#   OCUDU_NATIVE_V2X_EPISODES        0 (episodes back to back until the gate ends)
#   OCUDU_NATIVE_V2X_DRIVE_EXTRA     extra drive.py words, e.g. "--fallback none"
#   OCUDU_NATIVE_MUE_DURATION_SECONDS 420 (broker --duration; attach takes 60-150 s)
#   V2X_PYTHON                       ~/venvs/v2x/bin/v2x-python (setup-external.sh)
# plus every knob of the multi-UE gate (OCUDU_NATIVE_SIONNA_*, OCUDU_NATIVE_MUE_*).
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
: "${OCUDU_NATIVE_ROOT:?set OCUDU_NATIVE_ROOT (native workspace)}"
: "${OCUDU_NATIVE_SIONNA_PYTHON:?set OCUDU_NATIVE_SIONNA_PYTHON to the Sionna interpreter}"
site="${OCUDU_NATIVE_V2X_SITE:-blocked}"
scenario="${repo_root}/examples/configs/sionna/scenarios/v2x_intersection/v2x-intersection-${site}.json"
[[ -f "${scenario}" ]] || { echo "error: no scenario for site '${site}': ${scenario}" >&2; exit 2; }
v2x_python="${V2X_PYTHON:-${HOME}/venvs/v2x/bin/v2x-python}"
[[ -x "${v2x_python}" ]] || {
  echo "error: ${v2x_python} missing; run examples/v2x_intersection/setup-external.sh" >&2; exit 2; }

# Short fixed paths (AF_UNIX / ipc:// paths must stay under 107 bytes). Cleared
# per run so a ready file of an earlier run cannot release the episodes early.
v2x_run="${OCUDU_NATIVE_ROOT}/run/v2x"
rm -rf "${v2x_run}"
mkdir -p "${v2x_run}"
positions="ipc://${v2x_run}/positions.sock"

export OCUDU_NATIVE_SIONNA_SCENARIO="${scenario}"
export OCUDU_NATIVE_SIONNA_POSITION_ENDPOINT="${positions}"
export OCUDU_NATIVE_SIONNA_POSITION_OFFSET="${OCUDU_NATIVE_SIONNA_POSITION_OFFSET:-0,0,0}"
export OCUDU_NATIVE_MUE_DURATION_SECONDS="${OCUDU_NATIVE_MUE_DURATION_SECONDS:-420}"
export OCUDU_NATIVE_MUE_ROOT_EXEC="V2X_PYTHON='${v2x_python}' V2X_POSITIONS_ENDPOINT='${positions}' \
V2X_RUN='${v2x_run}' V2X_MODES='${OCUDU_NATIVE_V2X_MODES:-v2x,no-v2x}' \
V2X_EPISODES='${OCUDU_NATIVE_V2X_EPISODES:-0}' V2X_DRIVE_EXTRA='${OCUDU_NATIVE_V2X_DRIVE_EXTRA:-}' \
bash '${script_dir}/launch/root_exec.sh' {run_dir} {log_dir} '{ue_ids}' '{ue_ips}' {ue_gateway}"
export OCUDU_NATIVE_MUE_UE_EXEC="V2X_RUN='${v2x_run}' \
bash '${script_dir}/launch/ue_exec.sh' {ue_id} {ue_ip} {ue_index} {run_dir} {log_dir}"
exec bash "${repo_root}/scripts/native/run-ocudu-sionna-multi-ue.sh" "$@"
