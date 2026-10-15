#!/usr/bin/env bash
# Rescue glue for the native multi-UE gate's OCUDU_NATIVE_MUE_ROOT_EXEC hook.
#
# Runs in the gate's stack namespace (ogstun 10.45.1.1, the broker and the
# Sionna bridge live there). Waits until every robot's UE link (started by
# ue_exec.sh inside its UE namespace once the UE has pinged the core) is
# ready, then runs episodes until RESCUE_EPISODES are done or the gate tears
# this group down. Per episode: mission_server.py bound to the UPF-side
# address (its replies route back over GTP to each UE), sim.py publishing the
# robots' positions on RESCUE_POSITIONS_ENDPOINT (the endpoint the gate's
# bridge subscribes to), and one robot.py per robot.
#
# Usage (as the gate expands it):
#   bash root_exec.sh {run_dir} {log_dir} '{ue_ids}' '{ue_ips}' {ue_gateway}
#
# Knobs: RESCUE_PYTHON (~/venvs/rescue/bin/rescue-python), RESCUE_POSITIONS_ENDPOINT
# (required, ipc://), RESCUE_RUN (sockets, ready files, queues; shared with
# ue_exec.sh), RESCUE_ROBOTS (3), RESCUE_SITE (entrance), RESCUE_SERVER_PORT
# (7200), RESCUE_EPISODES (1; 0 = until killed), RESCUE_WORLD (kinematic),
# RESCUE_SURVIVORS (demo), RESCUE_TIME_LIMIT (600), RESCUE_GAP_S (5),
# RESCUE_UE_WAIT_S (300), RESCUE_SIM_EXTRA (extra sim.py words).
set -euo pipefail

run_dir="$1"; log_dir="$2"; ue_ids_text="$3"; ue_ips_text="$4"; gateway="$5"
read -r -a ue_ids <<<"${ue_ids_text}"
read -r -a ue_ips <<<"${ue_ips_text}"
python="${RESCUE_PYTHON:-${HOME}/venvs/rescue/bin/rescue-python}"
[[ -x "${python}" ]] || { echo "error: RESCUE_PYTHON not executable: ${python}" >&2; exit 2; }
positions="${RESCUE_POSITIONS_ENDPOINT:?set RESCUE_POSITIONS_ENDPOINT (ipc://...)}"
rescue_run="${RESCUE_RUN:?set RESCUE_RUN}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pkg_dir="$(dirname "${script_dir}")"
robots="${RESCUE_ROBOTS:-3}"
port="${RESCUE_SERVER_PORT:-7200}"
episodes="${RESCUE_EPISODES:-1}"
out="${log_dir}/rescue"
mkdir -p "${out}/episodes" "${rescue_run}"

now_ms() { date -u +%s%3N; }
log() { printf '%s event=%s\n' "$(now_ms)" "$*"; }

pids=()
stop_children()
{
  local pid
  for pid in "${pids[@]}"; do kill -s TERM "${pid}" 2>/dev/null || true; done
  for pid in "${pids[@]}"; do wait "${pid}" 2>/dev/null || true; done
  pids=()
}
trap 'log stop signal=TERM; stop_children; exit 143' TERM
trap 'log stop signal=INT; stop_children; exit 130' INT

robot_ids=()
for ((i = 0; i < robots; i++)); do robot_ids+=("robot_$((i + 1))"); done
log start ue_ids="${ue_ids[*]}" ue_ips="${ue_ips[*]}" gateway="${gateway}" robots="${robot_ids[*]}" \
  positions="${positions}"

deadline=$((SECONDS + ${RESCUE_UE_WAIT_S:-300}))
for robot in "${robot_ids[@]}"; do
  while [[ ! -f "${rescue_run}/link-${robot}.ready" ]]; do
    if [[ "${SECONDS}" -ge "${deadline}" ]]; then log ue_link_timeout robot="${robot}"; exit 3; fi
    sleep 0.5
  done
done
log ue_links_ready

if [[ "${RESCUE_WORLD:-kinematic}" == isaac ]]; then
  # Writable Kit root for this run, seeded with the venv's warm shader/derived-data cache
  # (see isaac_world.py: without it the namespaced root recompiles shaders on every render).
  kit_cache="$("${python}" -c 'import isaacsim, os; print(os.path.join(os.path.dirname(isaacsim.__file__), "kit", "cache"))')"
  export RESCUE_KIT_ROOT="${rescue_run}/kit"
  mkdir -p "${RESCUE_KIT_ROOT}"
  # Plain copy: inside the gate's user namespace ownership cannot be preserved, and a few
  # dev-private entries (instance locks, the GL shader cache) are unreadable; those are skipped
  # and counted, the shader and derived-data caches the renderer needs copy fine.
  copy_errors=0
  if [[ ! -d "${RESCUE_KIT_ROOT}/cache" ]]; then
    copy_errors="$(cp -r --no-preserve=all "${kit_cache}" "${RESCUE_KIT_ROOT}/cache" 2>&1 | wc -l || true)"
  fi
  log kit_root path="${RESCUE_KIT_ROOT}" seeded_from="${kit_cache}" copy_errors="${copy_errors}" \
    size="$(du -sh "${RESCUE_KIT_ROOT}/cache" 2>/dev/null | cut -f1)"
fi

episode=0
while [[ "${episodes}" -eq 0 || "${episode}" -lt "${episodes}" ]]; do
  dir="${out}/episodes/$(printf 'e%03d-live' "${episode}")"
  mkdir -p "${dir}"
  ln -sfn ../../links "${dir}/links"
  mission="rescue_$(date -u +%Y%m%dT%H%M%SZ)_$(printf '%03d' "${episode}")"
  state="${rescue_run}/central_state.json"
  rm -f "${state}"
  cat >"${dir}/run.json" <<EOF
{"mission_id": "${mission}", "network": "live", "network_mode": "gpu_iq_live_5g",
 "radio": "OCUDU gNB + srsUE + Open5GS via the native multi-UE Sionna gate", "robots": ${robots},
 "site": "${RESCUE_SITE:-entrance}", "world": "${RESCUE_WORLD:-kinematic}", "survivors": "${RESCUE_SURVIVORS:-demo}",
 "ue_ids": "${ue_ids[*]}", "ue_ips": "${ue_ips[*]}", "gateway": "${gateway}", "gate_run_dir": "${run_dir}",
 "started_ms": $(now_ms)}
EOF
  echo "{\"run_dir\": \"${rescue_run}\", \"episode_dir\": \"${dir}\", \"mission_id\": \"${mission}\", \"network_mode\": \"gpu_iq_live_5g\"}" \
    >"${rescue_run}/current.json"
  log episode_start n="${episode}" dir="${dir}" mission="${mission}"
  "${python}" "${pkg_dir}/mission_server.py" --bind "${gateway}:${port}" --mission-id "${mission}" \
    --out "${dir}/server" --state-json "${state}" >"${dir}/mission_server.out" 2>&1 &
  pids+=("$!")
  # shellcheck disable=SC2086
  "${python}" "${pkg_dir}/sim.py" --out "${dir}/sim" --run-dir "${rescue_run}" \
    --world "${RESCUE_WORLD:-kinematic}" --survivors "${RESCUE_SURVIVORS:-demo}" --robots "${robots}" \
    --positions-endpoint "${positions}" --central-state "${state}" --time-limit "${RESCUE_TIME_LIMIT:-600}" \
    --label gpu_iq_live_5g --frames-dir "${rescue_run}/frames" ${RESCUE_SIM_EXTRA:-} >"${dir}/sim.out" 2>&1 &
  sim_pid="$!"
  sleep 0.5
  for robot in "${robot_ids[@]}"; do
    "${python}" "${pkg_dir}/robot.py" --robot-id "${robot}" --mission-id "${mission}" \
      --body-socket "${rescue_run}/body-${robot}.sock" --own-socket "${rescue_run}/robot-${robot}.sock" \
      --link-socket "${rescue_run}/link-${robot}.sock" --log-dir "${dir}/robots" \
      --status-json "${rescue_run}/robot-${robot}.json" \
      --max-seconds "$(( ${RESCUE_TIME_LIMIT:-600} + 30 ))" >"${dir}/robot-${robot}.out" 2>&1 &
    pids+=("$!")
  done
  set +e
  wait "${sim_pid}"; status="$?"
  set -e
  sleep 5          # let the links deliver what is still queued
  stop_children
  "${python}" "${pkg_dir}/analyze.py" "${dir}" >"${dir}/analyze.out" 2>&1 || true
  log episode_end n="${episode}" status="${status}" summary="$(tail -n 1 "${dir}/sim.out" 2>/dev/null)"
  episode=$((episode + 1))
  [[ "${episodes}" -eq 0 || "${episode}" -lt "${episodes}" ]] && sleep "${RESCUE_GAP_S:-5}"
done
log done episodes="${episode}"
