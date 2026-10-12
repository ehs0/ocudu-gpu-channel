#!/usr/bin/env bash
# V2X glue for the native multi-UE gate's OCUDU_NATIVE_MUE_ROOT_EXEC hook.
#
# Runs in the gate's stack namespace (ogstun 10.45.1.1, the broker and the
# Sionna bridge live there). Starts the roadside server bound to the UPF-side
# address, so its datagrams to vehicle A's UE go core -> gNB -> channel
# emulator -> UE; waits until A's UE client (started by ue_exec.sh inside the
# UE namespace once the UE has pinged the core) is bound; then runs episodes
# back to back until the gate tears this group down. Each episode is one
# drive.py: SUMO + A's controller, publishing A's position to the bridge on
# V2X_POSITIONS_ENDPOINT (the endpoint the gate's bridge subscribes to).
#
# Usage (as the gate expands it):
#   bash root_exec.sh {run_dir} {log_dir} '{ue_ids}' '{ue_ips}' {ue_gateway}
#
# Knobs: V2X_PYTHON (~/venvs/v2x/bin/v2x-python), V2X_POSITIONS_ENDPOINT
# (required, ipc://), V2X_RUN (directory for the relay socket and the ready
# file, shared with ue_exec.sh), V2X_PORT (7100), V2X_MODES (v2x,no-v2x:
# cycled per episode; v2x | no-v2x, optionally :cautious | :none for the
# fallback), V2X_EPISODES (0 = until killed), V2X_GAP_S (3), V2X_UE_WAIT_S
# (300), V2X_DRIVE_EXTRA (extra drive.py words).
set -euo pipefail

run_dir="$1"; log_dir="$2"; ue_ids_text="$3"; ue_ips_text="$4"; gateway="$5"
read -r -a ue_ids <<<"${ue_ids_text}"
read -r -a ue_ips <<<"${ue_ips_text}"
python="${V2X_PYTHON:-${HOME}/venvs/v2x/bin/v2x-python}"
[[ -x "${python}" ]] || { echo "error: V2X_PYTHON not executable: ${python}" >&2; exit 2; }
positions="${V2X_POSITIONS_ENDPOINT:?set V2X_POSITIONS_ENDPOINT (ipc://...)}"
v2x_run="${V2X_RUN:?set V2X_RUN}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pkg_dir="$(dirname "${script_dir}")"
port="${V2X_PORT:-7100}"
IFS=, read -r -a modes <<<"${V2X_MODES:-v2x,no-v2x}"
episodes="${V2X_EPISODES:-0}"
out="${log_dir}/v2x"
mkdir -p "${out}/episodes" "${v2x_run}"

now_ms() { date -u +%s%3N; }
log() { printf '%s event=%s\n' "$(now_ms)" "$*"; }

roadside_pid=""
drive_pid=""
stop_children()
{
  local pid
  for pid in "${drive_pid}" "${roadside_pid}"; do
    [[ -n "${pid}" ]] && kill -s TERM "${pid}" 2>/dev/null || true
  done
  for pid in "${drive_pid}" "${roadside_pid}"; do
    [[ -n "${pid}" ]] && wait "${pid}" 2>/dev/null || true
  done
}
trap 'log stop signal=TERM; stop_children; exit 143' TERM
trap 'log stop signal=INT; stop_children; exit 130' INT

log start ue_ids="${ue_ids[*]}" ue_ips="${ue_ips[*]}" gateway="${gateway}" positions="${positions}" \
  modes="${modes[*]}" sumo="$("${python}" -c 'import libsumo; print(libsumo.getVersion()[1])')"

"${python}" "${pkg_dir}/roadside.py" --positions-endpoint "${positions}" \
  --dest "${ue_ips[0]}:${port}" --bind "${gateway}:0" --log "${out}/roadside_tx.csv" \
  >"${out}/roadside.out" 2>&1 &
roadside_pid="$!"

deadline=$((SECONDS + ${V2X_UE_WAIT_S:-300}))
while [[ ! -f "${v2x_run}/ue-ready" ]]; do
  if [[ "${SECONDS}" -ge "${deadline}" ]]; then
    log ue_client_timeout; stop_children; exit 3
  fi
  sleep 0.5
done
log ue_client_ready

episode=0
while [[ "${episodes}" -eq 0 || "${episode}" -lt "${episodes}" ]]; do
  # A mode is "v2x" or "no-v2x", optionally ":<fallback>" (cautious | none).
  mode="${modes[$((episode % ${#modes[@]}))]}"
  dir="${out}/episodes/$(printf 'e%03d-%s' "${episode}" "${mode//:/-}")"
  extra=()
  [[ "${mode%%:*}" == "no-v2x" ]] && extra+=(--no-v2x)
  [[ "${mode}" == *:* ]] && extra+=(--fallback "${mode#*:}")
  log episode_start n="${episode}" mode="${mode}" dir="${dir}"
  # shellcheck disable=SC2086
  "${python}" "${pkg_dir}/drive.py" --out "${dir}" --v2x-socket "${v2x_run}/a-relay.sock" \
    --positions-endpoint "${positions}" --a-node "${ue_ids[0]}" --label "${mode}" \
    "${extra[@]}" ${V2X_DRIVE_EXTRA:-} >"${dir}.out" 2>&1 &
  drive_pid="$!"
  set +e
  wait "${drive_pid}"; status="$?"
  set -e
  drive_pid=""
  log episode_end n="${episode}" status="${status}" summary="$(tail -n 1 "${dir}.out" 2>/dev/null)"
  episode=$((episode + 1))
  sleep "${V2X_GAP_S:-3}"
done
stop_children
log done episodes="${episode}"
