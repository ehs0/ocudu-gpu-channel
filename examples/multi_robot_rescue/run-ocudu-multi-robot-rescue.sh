#!/usr/bin/env bash
# Multi-robot search and rescue over the live 5G link (native multi-UE gate, Sionna RT).
#
# One OCUDU gNB, srsUEs, Open5GS, this tree's broker and the Sionna bridge
# (scripts/native/run-ocudu-sionna-multi-ue.sh). Robot i carries UE ue{i-1}:
# the bridge follows the robots' positions from sim.py's positions feed. The
# gate's hooks add the application: launch/ue_exec.sh starts each robot's UE
# link (ue_link.py) inside that UE's network namespace, bound to its TUN
# address; launch/root_exec.sh starts the mission server on the UPF-side
# address and runs episodes (sim.py + robot.py per robot). Every byte between a
# robot and the mission server crosses UE -> channel emulator -> gNB -> core.
#
# The gate runs 2 or 4 UEs: 1 or 2 robots use 2 UEs, 3 robots use 4 (the
# UEs without a robot are parked outside in line of sight and carry no
# application traffic).
#
# Run from the repo root, as root (the gate creates network namespaces):
#   bash examples/multi_robot_rescue/run-ocudu-multi-robot-rescue.sh
#
# Knobs (defaults):
#   OCUDU_NATIVE_RESCUE_ROBOTS        3 (1, 2 or 3 robots on live UEs)
#   OCUDU_NATIVE_RESCUE_SITE          entrance | street (entrance): radio site, picks the scenario JSON
#   OCUDU_NATIVE_RESCUE_EPISODES      1 (0 = back to back until the gate ends)
#   OCUDU_NATIVE_RESCUE_WORLD         kinematic | isaac (kinematic)
#   OCUDU_NATIVE_RESCUE_SURVIVORS     demo (preset name, or random)
#   OCUDU_NATIVE_RESCUE_TIME_LIMIT    600 (s per episode; it ends earlier on success)
#   OCUDU_NATIVE_RESCUE_SIM_EXTRA     extra sim.py words
#   OCUDU_NATIVE_RESCUE_WEB_PORT      8080: the rescue dashboard (web/server.py); 0 = none
#   OCUDU_NATIVE_WEB_PORT             8081 here: the gate's shared web UI (radio panels)
#   OCUDU_NATIVE_MUE_DURATION_SECONDS 900 (broker --duration; attach takes 60-150 s)
#   RESCUE_PYTHON                     ~/venvs/rescue/bin/rescue-python (setup-external.sh)
# plus every knob of the multi-UE gate (OCUDU_NATIVE_SIONNA_*, OCUDU_NATIVE_MUE_*).
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
: "${OCUDU_NATIVE_ROOT:?set OCUDU_NATIVE_ROOT (native workspace)}"
: "${OCUDU_NATIVE_SIONNA_PYTHON:?set OCUDU_NATIVE_SIONNA_PYTHON to the Sionna interpreter}"
robots="${OCUDU_NATIVE_RESCUE_ROBOTS:-3}"
[[ "${robots}" =~ ^[123]$ ]] || { echo "error: OCUDU_NATIVE_RESCUE_ROBOTS must be 1, 2 or 3" >&2; exit 2; }
site="${OCUDU_NATIVE_RESCUE_SITE:-entrance}"
scenario="${repo_root}/examples/configs/sionna/scenarios/multi_robot_rescue/multi-robot-rescue-${site}-${robots}robot.json"
[[ -f "${scenario}" ]] || { echo "error: no scenario for site '${site}', ${robots} robots: ${scenario}" >&2; exit 2; }
rescue_python="${RESCUE_PYTHON:-${HOME}/venvs/rescue/bin/rescue-python}"
[[ -x "${rescue_python}" ]] || {
  echo "error: ${rescue_python} missing; run examples/multi_robot_rescue/setup-external.sh" >&2; exit 2; }

# Short fixed paths (AF_UNIX / ipc:// paths must stay under 107 bytes). Cleared
# per run so a ready file of an earlier run cannot start the episodes early.
rescue_run="${OCUDU_NATIVE_ROOT}/run/rescue"
rm -rf "${rescue_run}"
mkdir -p "${rescue_run}"
positions="ipc://${rescue_run}/positions.sock"

export OCUDU_NATIVE_SIONNA_SCENARIO="${scenario}"
export OCUDU_NATIVE_SIONNA_POSITION_ENDPOINT="${positions}"
export OCUDU_NATIVE_SIONNA_POSITION_OFFSET="${OCUDU_NATIVE_SIONNA_POSITION_OFFSET:-0,0,0}"
export OCUDU_NATIVE_MUE_UE_COUNT="$([[ "${robots}" == 3 ]] && echo 4 || echo 2)"
export OCUDU_NATIVE_MUE_DURATION_SECONDS="${OCUDU_NATIVE_MUE_DURATION_SECONDS:-900}"
export OCUDU_NATIVE_MUE_ROOT_EXEC="RESCUE_PYTHON='${rescue_python}' RESCUE_POSITIONS_ENDPOINT='${positions}' \
RESCUE_RUN='${rescue_run}' RESCUE_ROBOTS='${robots}' RESCUE_SITE='${site}' \
RESCUE_EPISODES='${OCUDU_NATIVE_RESCUE_EPISODES:-1}' RESCUE_WORLD='${OCUDU_NATIVE_RESCUE_WORLD:-kinematic}' \
RESCUE_SURVIVORS='${OCUDU_NATIVE_RESCUE_SURVIVORS:-demo}' RESCUE_TIME_LIMIT='${OCUDU_NATIVE_RESCUE_TIME_LIMIT:-600}' \
RESCUE_SIM_EXTRA='${OCUDU_NATIVE_RESCUE_SIM_EXTRA:-}' \
bash '${script_dir}/launch/root_exec.sh' {run_dir} {log_dir} '{ue_ids}' '{ue_ips}' {ue_gateway}"
export OCUDU_NATIVE_MUE_UE_EXEC="RESCUE_RUN='${rescue_run}' RESCUE_ROBOTS='${robots}' \
bash '${script_dir}/launch/ue_exec.sh' {ue_id} {ue_ip} {ue_index} {run_dir} {log_dir}"
export OCUDU_NATIVE_WEB_PORT="${OCUDU_NATIVE_WEB_PORT:-8081}"
web_port="${OCUDU_NATIVE_RESCUE_WEB_PORT:-8080}"
web_pid=""
if [[ "${web_port}" != 0 ]] && (exec 3<>"/dev/tcp/127.0.0.1/${web_port}") 2>/dev/null; then
  echo "error: port ${web_port} is already in use (another dashboard?); free it or set OCUDU_NATIVE_RESCUE_WEB_PORT" >&2
  exit 2
fi
if [[ "${web_port}" != 0 ]]; then
  "${rescue_python}" "${script_dir}/web/server.py" --port "${web_port}" --run-dir "${rescue_run}" \
    --positions-endpoint "${positions}" --radio-ui "http://127.0.0.1:${OCUDU_NATIVE_WEB_PORT}" \
    >"${rescue_run}/web.log" 2>&1 &
  web_pid="$!"
  printf 'Rescue dashboard: http://127.0.0.1:%s (radio panels: the shared UI on %s)\n' \
    "${web_port}" "${OCUDU_NATIVE_WEB_PORT}"
fi
set +e
bash "${repo_root}/scripts/native/run-ocudu-sionna-multi-ue.sh" "$@"
status="$?"
set -e
[[ -n "${web_pid}" ]] && kill "${web_pid}" 2>/dev/null
exit "${status}"
