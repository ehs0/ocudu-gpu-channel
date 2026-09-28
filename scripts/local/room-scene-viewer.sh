#!/usr/bin/env bash
# View the room/table scene and the live node positions in a browser, without
# running a gNB, a UE or the 5G core.
#
# The full smoke test starts a Web UI too, but it dies with the run and needs
# Open5GS and four containers first. This starts only what the picture needs:
# the CUDA broker (so the bridge has somewhere to send profiles), the Sionna
# bridge, and the read-only web server. Nothing here transmits IQ.
#
#   scripts/local/room-scene-viewer.sh
#   scripts/local/room-scene-viewer.sh --position-endpoint tcp://127.0.0.1:5601
#
# The second form follows robot poses published by Isaac Sim. Without it the
# nodes sit wherever the scenario config's start_m puts them.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

python_bin="${OCUDU_SIONNA_PYTHON:-/home/hyunsoo/OCUDU/venvs/sionna/bin/python}"
scenario="${OCUDU_ROOM_SCENARIO:-${repo_root}/examples/sionna/room-table-2gnb-2ue.json}"
# The 4-antenna-per-cell topology: the room scenario declares 1x4 gNB arrays,
# and the broker rejects a profile whose dimensions differ from the link it
# prepared.
topology="${OCUDU_ROOM_TOPOLOGY:-${repo_root}/examples/topology.sionna-multi-gnb.cuda.yaml}"
broker_bin="${OCUDU_BROKER_BIN:-${repo_root}/build/ocudu-gpu-channel}"
port="${OCUDU_ROOM_PORT:-8080}"
update_hz="${OCUDU_ROOM_UPDATE_HZ:-10}"
log_dir="${OCUDU_ROOM_LOG_DIR:-${TMPDIR:-/tmp}/ocudu-room-viewer}"

[[ -x "${python_bin}" ]] || { echo "Sionna python not executable: ${python_bin}" >&2; exit 2; }
[[ -x "${broker_bin}" ]] || { echo "broker not built: ${broker_bin} (cmake --build build)" >&2; exit 2; }
[[ -f "${scenario}" ]] || { echo "missing scenario: ${scenario}" >&2; exit 2; }

mkdir -p "${log_dir}"
status_jsonl="${log_dir}/status.jsonl"
# A fresh file each run: the UI reads the tail, and a stale one shows the
# previous run's geometry until the first new record lands.
: > "${status_jsonl}"

broker_pid=""
bridge_pid=""
web_pid=""
cleanup() {
  local status="$?"
  trap - EXIT INT TERM HUP
  for pid in "${web_pid}" "${bridge_pid}" "${broker_pid}"; do
    [[ -n "${pid}" ]] && kill -TERM "${pid}" >/dev/null 2>&1 || true
  done
  for pid in "${web_pid}" "${bridge_pid}" "${broker_pid}"; do
    [[ -n "${pid}" ]] && wait "${pid}" >/dev/null 2>&1 || true
  done
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"${broker_bin}" --config "${topology}" --duration 0s \
  --control-endpoint 'tcp://*:5559' \
  --telemetry-endpoint 'tcp://*:5560' --telemetry-rate-hz 20 \
  > "${log_dir}/broker.log" 2>&1 &
broker_pid="$!"

# The broker binds its control socket during startup; the bridge's first
# request fails outright if it arrives before then.
for _ in $(seq 1 40); do
  ss -ltn 2>/dev/null | grep -q ':5559' && break
  sleep 0.25
done

"${python_bin}" "${repo_root}/scripts/sionna_rt/run_bridge.py" \
  --scenario-config "${scenario}" \
  --control-endpoint tcp://127.0.0.1:5559 \
  --duration 0 --update-hz "${update_hz}" \
  --max-depth 3 --samples-per-src 50000 \
  --status-jsonl "${status_jsonl}" "$@" \
  > "${log_dir}/bridge.log" 2>&1 &
bridge_pid="$!"

"${python_bin}" "${repo_root}/scripts/web_ui/server.py" \
  --bind 127.0.0.1 --port "${port}" \
  --telemetry-endpoint tcp://127.0.0.1:5560 \
  --status-jsonl "${status_jsonl}" \
  --index "${repo_root}/scripts/web_ui/index.html" \
  > "${log_dir}/web.log" 2>&1 &
web_pid="$!"

for _ in $(seq 1 240); do
  kill -0 "${bridge_pid}" 2>/dev/null || { echo "bridge exited:" >&2; tail -20 "${log_dir}/bridge.log" >&2; exit 1; }
  kill -0 "${web_pid}" 2>/dev/null || { echo "web UI exited:" >&2; tail -20 "${log_dir}/web.log" >&2; exit 1; }
  if grep -q '"event":"sionna_rt_update"' "${status_jsonl}" 2>/dev/null; then
    echo
    echo "  방 뷰어 준비 완료:  http://127.0.0.1:${port}"
    echo "  시나리오: ${scenario}"
    echo "  로그:     ${log_dir}"
    echo "  종료:     Ctrl-C"
    echo
    break
  fi
  sleep 0.25
done

wait "${bridge_pid}"
