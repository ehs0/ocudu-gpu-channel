#!/usr/bin/env bash
# Inner half of run-single-cell-policy.sh. Runs INSIDE the user + network +
# mount namespace that script creates; never invoke it directly.
#
# Process supervision, namespace layout, core start-up and bounded shutdown
# follow run-ocudu-scheduler-benchmark-inner.sh (left unchanged). What differs:
# one cell, N UEs, one broker, and the replay bridge instead of a live Sionna
# solve; the measurement is released by touching the hold file once every UE
# has attached, and it ends when the replay bridge reports that every link has
# passed sample S0 + K * samples_per_update (radio time), not after a number
# of wall seconds.
set -uo pipefail

repo_root="" native_root="" run_root="" parent_netns="" parent_mntns="" outer_uid=""
gnb="" broker="" ue_count="" policy="" channel_bundle="" traffic_bundle="" sionna_python=""
attach_timeout="240" drain_seconds="6" gnb_start_timeout="20"
cpus_gnb="" cpus_broker="" cpus_ues="" cpus_aux=""

usage_error() { printf 'error: %s\n' "$1" >&2; exit 2; }

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --repo-root) repo_root="${2:-}"; shift 2 ;;
    --native-root) native_root="${2:-}"; shift 2 ;;
    --run-root) run_root="${2:-}"; shift 2 ;;
    --parent-netns) parent_netns="${2:-}"; shift 2 ;;
    --parent-mntns) parent_mntns="${2:-}"; shift 2 ;;
    --outer-uid) outer_uid="${2:-}"; shift 2 ;;
    --gnb-binary) gnb="${2:-}"; shift 2 ;;
    --broker) broker="${2:-}"; shift 2 ;;
    --ue-count) ue_count="${2:-}"; shift 2 ;;
    --policy) policy="${2:-}"; shift 2 ;;
    --channel-bundle) channel_bundle="${2:-}"; shift 2 ;;
    --traffic-bundle) traffic_bundle="${2:-}"; shift 2 ;;
    --sionna-python) sionna_python="${2:-}"; shift 2 ;;
    --attach-timeout) attach_timeout="${2:-}"; shift 2 ;;
    --drain-seconds) drain_seconds="${2:-}"; shift 2 ;;
    --cpus-gnb) cpus_gnb="${2:-}"; shift 2 ;;
    --cpus-broker) cpus_broker="${2:-}"; shift 2 ;;
    --cpus-ues) cpus_ues="${2:-}"; shift 2 ;;
    --cpus-aux) cpus_aux="${2:-}"; shift 2 ;;
    *) usage_error "unexpected argument: $1" ;;
  esac
done
for value in "${repo_root}" "${native_root}" "${run_root}" "${parent_netns}" "${parent_mntns}" "${outer_uid}" \
             "${gnb}" "${broker}" "${ue_count}" "${policy}" "${channel_bundle}" "${traffic_bundle}" "${sionna_python}"; do
  [[ -n "${value}" ]] || usage_error "missing required argument"
done
[[ "$(readlink /proc/self/ns/net)" != "${parent_netns}" ]] || usage_error "network namespace was not isolated"
[[ "$(readlink /proc/self/ns/mnt)" != "${parent_mntns}" ]] || usage_error "mount namespace was not isolated"
awk -v uid="${outer_uid}" '$1 == 0 && $2 == uid && $3 == 1 { found = 1 } END { exit !found }' \
  /proc/self/uid_map || usage_error "user namespace does not contain the exact root mapping"

bench_dir="${repo_root}/examples/scheduler_benchmark"
# Every process of this run inherits its run root: the controller identifies
# (and, after a failed stop, kills) a session's processes by it.
export SINGLE_CELL_RUN_ROOT="${run_root}"
config_dir="${run_root}/configs" log_dir="${run_root}/logs" report_dir="${run_root}/report"
data_dir="${run_root}/data" netns_dir="${run_root}/run/netns"
# ipc:// paths are limited to ~107 bytes; keep the sockets short and private.
sock_dir="$(mktemp -d /tmp/scb.XXXXXX)" || usage_error "cannot create the socket directory"
control_endpoint="ipc://${sock_dir}/control.sock"
telemetry_endpoint="ipc://${sock_dir}/telemetry.sock"
hold_file="${run_root}/run/measure.start"
epoch_file="${run_root}/run/measure-epoch.json"
traffic_stop_file="${run_root}/run/traffic.stop"
ue_ids=() ue_netns=()
for ((i = 0; i < ue_count; i++)); do ue_ids+=("ue${i}"); ue_netns+=("ue$((i + 1))"); done
ue_gateway="10.45.1.1"

mount_active=0
root_tun=""
declare -a created_netns=() process_names=() process_pids=() process_pgids=()
started_pid=""

process_running() {
  local state
  state="$(ps -o stat= -p "$1" 2>/dev/null | awk '{print $1}')"
  [[ -n "${state}" && "${state:0:1}" != "Z" ]]
}
start_group() {
  local name="$1" output="$2"; shift 2
  local gate_lock_fd="${OCUDU_NATIVE_GATE_LOCK_FD:-}"
  if [[ "${gate_lock_fd}" =~ ^[0-9]+$ && "${gate_lock_fd}" -gt 2 ]]; then
    setsid stdbuf -oL -eL "$@" >"${output}" 2>&1 {gate_lock_fd}>&- &
  else
    setsid stdbuf -oL -eL "$@" >"${output}" 2>&1 &
  fi
  started_pid="$!"
  process_names+=("${name}"); process_pids+=("${started_pid}"); process_pgids+=("${started_pid}")
}
group_alive() {
  ps -eo pgid=,sid=,stat= | awk -v g="$1" '($1 == g || $2 == g) && $3 !~ /^Z/ { found = 1 } END { exit !found }'
}
signal_group() {
  local signal="$1" pgid="$2" member
  kill -s "${signal}" -- "-${pgid}" >/dev/null 2>&1 || true
  for member in $(ps -eo pid=,sid= | awk -v g="${pgid}" '$2 == g { print $1 }'); do
    kill -s "${signal}" "${member}" >/dev/null 2>&1 || true
  done
}
stop_group() {
  local index="$1" pid="${process_pids[$1]}" pgid="${process_pgids[$1]}" signal deadline
  [[ "${pid}" =~ ^[1-9][0-9]*$ && "${pgid}" =~ ^[1-9][0-9]*$ && "${pgid}" -gt 1 ]] || return 0
  if group_alive "${pgid}"; then
    for signal in INT TERM KILL; do
      signal_group "${signal}" "${pgid}"
      deadline=$((SECONDS + 4))
      while group_alive "${pgid}" && [[ "${SECONDS}" -lt "${deadline}" ]]; do sleep 0.1; done
      group_alive "${pgid}" || break
    done
    if group_alive "${pgid}"; then
      printf 'error: %s group %s remains alive after bounded KILL\n' "${process_names[index]}" "${pgid}" >&2
      return 124
    fi
  fi
  wait "${pid}" >/dev/null 2>&1 || true
  return 0
}
stop_named() {
  local wanted="$1" index failed=0
  for ((index = 0; index < ${#process_pids[@]}; index++)); do
    if [[ "${process_names[index]}" == "${wanted}"* && "${process_pids[index]}" != "0" ]]; then
      if stop_group "${index}"; then process_pids[index]="0"; else failed=1; fi
    fi
  done
  return "${failed}"
}
cleanup() {
  local original_status="$?" index wanted cleanup_failed=0
  set +e
  # A stop arrives as several signals (process group, then unshare's
  # --kill-child=TERM); ignore the rest so the bounded cleanup finishes.
  trap - EXIT
  trap '' INT TERM HUP
  for wanted in traffic gnb-metrics-recorder broker srsue gnb-metrics-relay gnb replay open5gs mongod; do
    stop_named "${wanted}" || cleanup_failed=1
  done
  for ((index = ${#process_pids[@]} - 1; index >= 0; index--)); do stop_group "${index}" || cleanup_failed=1; done
  for name in "${created_netns[@]}"; do ip netns del "${name}" >/dev/null 2>&1 || true; done
  [[ -n "${root_tun}" ]] && { ip link del "${root_tun}" >/dev/null 2>&1 || true; }
  [[ "${mount_active}" -eq 1 ]] && { umount /run/netns >/dev/null 2>&1 || true; }
  rm -rf -- "${sock_dir}"
  [[ "${cleanup_failed}" -ne 0 && "${original_status}" -eq 0 ]] && original_status=124
  exit "${original_status}"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

wait_log() {
  local file="$1" pattern="$2" pid="$3" timeout="$4" deadline=$((SECONDS + $4))
  while [[ "${SECONDS}" -lt "${deadline}" ]]; do
    grep -q -- "${pattern}" "${file}" 2>/dev/null && return 0
    process_running "${pid}" || return 1
    sleep 0.2
  done
  return 1
}
pin() { [[ -n "$1" ]] && printf '%s\n' taskset -c "$1"; return 0; }

srsue="${native_root}/builds/srsran4g-zmq-local/srsue/src/srsue"
fivegc="${native_root}/builds/open5gs-v2.7.6/tests/app/5gc"
mongod="${native_root}/install/mongodb-6.0.29/bin/mongod"
add_users="${native_root}/src/ocudu/docker/open5gs/add_users.py"
subscriber_verify="${repo_root}/scripts/native/verify-open5gs-subscribers-multi.py"
for binary in "${gnb}" "${srsue}" "${fivegc}" "${mongod}" "${broker}"; do
  [[ -x "${binary}" ]] || usage_error "missing executable: ${binary}"
done
mapfile -t aux_pin < <(pin "${cpus_aux}")

# --- namespace layout -------------------------------------------------------
mount --make-rprivate /
mount --bind "${netns_dir}" /run/netns
mount_active=1
ip link set lo up
root_tun="ogstun"
ip tuntap add dev "${root_tun}" mode tun
ip addr add 10.45.0.1/24 dev "${root_tun}"
ip addr add 10.45.1.1/24 dev "${root_tun}"
ip link set "${root_tun}" up
for name in "${ue_netns[@]}"; do
  ip netns add "${name}"
  created_netns+=("${name}")
  nsenter --net="/run/netns/${name}" -- ip link set lo up
done

# --- core -------------------------------------------------------------------
start_group mongod "${log_dir}/mongod-console.log" "${aux_pin[@]}" "${mongod}" \
  --dbpath "${data_dir}" --bind_ip 127.0.0.1 --port 27017 --logpath "${log_dir}/mongod.log"
/usr/bin/python3 - <<'PY' || usage_error "mongod did not accept connections"
import socket, time
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", 27017), timeout=.25):
            raise SystemExit(0)
    except OSError:
        time.sleep(.25)
raise SystemExit(2)
PY
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="${native_root}/src/open5gs:${PYTHONPATH:-}" /usr/bin/python3 "${add_users}" \
  --mongodb 127.0.0.1 --mongodb_port 27017 --subscriber_data "${config_dir}/subscriber.csv" \
  >"${log_dir}/subscriber-insert.log" 2>&1 || usage_error "subscriber insert failed"
if [[ "${ue_count}" -ge 2 ]]; then
  /usr/bin/python3 "${subscriber_verify}" --subscriber-csv "${config_dir}/subscriber.csv" \
    >"${log_dir}/subscriber-verify.log" 2>&1 || usage_error "subscriber verification failed"
else
  echo "event=subscriber_verify_skipped reason=multi-UE verifier requires >= 2 records" >"${log_dir}/subscriber-verify.log"
fi
start_group open5gs "${log_dir}/open5gs.log" "${aux_pin[@]}" "${fivegc}" -c "${config_dir}/open5gs.yaml"
/usr/bin/python3 - <<'PY' || usage_error "Open5GS AMF did not come up"
import socket, time
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        with socket.create_connection(("127.0.0.20", 7777), timeout=.25):
            raise SystemExit(0)
    except OSError:
        time.sleep(.25)
raise SystemExit(2)
PY

# --- broker -----------------------------------------------------------------
mapfile -t broker_pin < <(pin "${cpus_broker}")
start_group broker "${log_dir}/broker.log" env -u CUDA_MPS_PIPE_DIRECTORY "CUDA_VISIBLE_DEVICES=${OCUDU_NATIVE_GPU_DEVICE:-0}" \
  "${broker_pin[@]}" "${broker}" --config "${config_dir}/topology.yaml" --duration 10800s \
  --control-endpoint "${control_endpoint}" --telemetry-endpoint "${telemetry_endpoint}" --telemetry-rate-hz 50
broker_pid="${started_pid}"
wait_log "${log_dir}/broker.log" 'event=control_start ' "${broker_pid}" 15 || usage_error "broker control server did not become ready"

# --- replay bridge: attach channel committed before any radio starts ---------
start_group replay "${log_dir}/replay-bridge.log" "${aux_pin[@]}" "${sionna_python}" "${bench_dir}/replay_bridge.py" \
  --bundle "${channel_bundle}" --control-endpoint "${control_endpoint}" --hold-until-file "${hold_file}" \
  --epoch-file "${epoch_file}" --applied-log "${log_dir}/replay-applied.jsonl"
replay_pid="${started_pid}"
wait_log "${log_dir}/replay-bridge.log" '"event":"replay_attach_committed"' "${replay_pid}" 60 || {
  tail -5 "${log_dir}/replay-bridge.log" >&2; usage_error "replay bridge did not commit the attach channel"; }

# --- gNB with its policy, and the proof that it loaded it --------------------
mapfile -t gnb_pin < <(pin "${cpus_gnb}")
start_group gnb "${log_dir}/gnb0-console.log" env "CUDA_VISIBLE_DEVICES=${OCUDU_NATIVE_GPU_DEVICE:-0}" \
  "${gnb_pin[@]}" "${gnb}" -c "${config_dir}/gnb0.yaml"
gnb_pid="${started_pid}"
wait_log "${log_dir}/gnb0-console.log" '==== gNB started ===' "${gnb_pid}" "${gnb_start_timeout}" || usage_error "gnb0 did not start"
/usr/bin/python3 - "${log_dir}" "${policy}" "${bench_dir}" >"${log_dir}/policy-check.json" <<'PY' \
  || { cat "${log_dir}/policy-check.json" >&2; usage_error "the gNB did not load the requested scheduler policy"; }
import json, pathlib, re, sys, time
log_dir, policy, bench_dir = sys.argv[1:]
sys.path.insert(0, bench_dir)
import definitions as d
deadline = time.monotonic() + 15
while True:
    text = (pathlib.Path(log_dir) / "gnb0-internal.log").read_text(errors="replace")
    start = text.find("gNB input configuration (only non-default values)")
    if (start >= 0 and "\n  scheduler:" in text[start:start + 20000]) or time.monotonic() > deadline:
        break
    time.sleep(0.5)
dump = text[start:start + 20000] if start >= 0 else ""
block = re.search(r"\n( *)policy:\n((?:\1  .*\n)+)", dump)
body = block.group(2) if block else ""
found = d.schedulers_in_config_dump(body)
ok = found == [policy]
print(json.dumps({"ok": ok, "gnbs": {"gnb0": {"requested": policy, "found_in_config_dump": found, "ok": ok,
                                               "policy_block": body}}}, indent=2))
raise SystemExit(0 if ok else 1)
PY
printf 'event=scheduler_policy_verified gnb0=%s\n' "${policy}"

metrics_socket="${sock_dir}/gnb-metrics.sock"
start_group gnb-metrics-relay "${log_dir}/gnb-metrics-relay.log" "${aux_pin[@]}" /usr/bin/python3 \
  "${repo_root}/scripts/native/gnb-metrics-relay.py" --socket "${metrics_socket}" --port 8001
start_group gnb-metrics-recorder "${log_dir}/gnb-metrics-recorder.log" "${aux_pin[@]}" /usr/bin/python3 \
  "${bench_dir}/gnb_metrics_recorder.py" --endpoint "ws+unix://${metrics_socket}" --out "${log_dir}/gnb-metrics.jsonl"
sleep 3

# --- UEs --------------------------------------------------------------------
mapfile -t ue_pin < <(pin "${cpus_ues}")
declare -a srsue_pids=()
for index in "${!ue_ids[@]}"; do
  id="${ue_ids[index]}"
  date -u +%s%3N >"${log_dir}/srsue-${id}.start_unix_ms"
  # Distinct contention preambles for up to 16 UEs (definitions.single_cell_prach_preamble).
  start_group "srsue-${id}" "${log_dir}/srsue-${id}.log" env "SRSUE_PRACH_PREAMBLE_INDEX=$((index * 4))" \
    "${ue_pin[@]}" "${srsue}" --general.metrics_csv_enable 1 --general.metrics_period_secs 1 \
    --general.metrics_csv_filename "${log_dir}/srsue-metrics-${id}.csv" "${config_dir}/srsue-${id}.conf"
  srsue_pids+=("${started_pid}")
done

# --- attach (the attach channel holds for as long as this takes) -------------
declare -a rrc=() pdu=() ping_ok=()
for _ in "${ue_ids[@]}"; do rrc+=(0); pdu+=(0); ping_ok+=(0); done
all_done=0
deadline=$((SECONDS + attach_timeout))
while [[ "${SECONDS}" -lt "${deadline}" ]]; do
  all_done=1
  for index in "${!ue_ids[@]}"; do
    log="${log_dir}/srsue-${ue_ids[index]}.log"
    grep -q 'RRC Connected' "${log}" 2>/dev/null && rrc[index]=1
    grep -q 'PDU Session Establishment successful' "${log}" 2>/dev/null && pdu[index]=1
    if [[ "${rrc[index]}" -eq 1 && "${pdu[index]}" -eq 1 && "${ping_ok[index]}" -eq 0 ]]; then
      nsenter --net="/run/netns/${ue_netns[index]}" -- \
        ping -I tun_srsue -c 3 -W 2 "${ue_gateway}" >"${log_dir}/ue-ping-${ue_ids[index]}.log" 2>&1 && ping_ok[index]=1
    fi
    [[ "${ping_ok[index]}" -eq 1 ]] || all_done=0
  done
  [[ "${all_done}" -eq 1 ]] && break
  sleep 0.5
done
attach_ok="${all_done}"
printf 'event=attach rrc=%s pdu=%s ping=%s ok=%s t=%s\n' "${rrc[*]}" "${pdu[*]}" "${ping_ok[*]}" "${attach_ok}" "${SECONDS}"

measure_status="skipped"
replay_status="not_started"
if [[ "${attach_ok}" -eq 1 ]]; then
  # Receivers first, then senders (they wait for the epoch file), then the release.
  start_group traffic-ul-recv "${log_dir}/traffic-ul-recv.log" "${aux_pin[@]}" "${sionna_python}" \
    "${bench_dir}/radio_traffic.py" ul-recv --bundle "${traffic_bundle}" --log "${log_dir}/rx-ul.bin" \
    --until-file "${traffic_stop_file}"
  for index in "${!ue_ids[@]}"; do
    start_group "traffic-dl-recv-${ue_ids[index]}" "${log_dir}/traffic-dl-recv-${ue_ids[index]}.log" "${aux_pin[@]}" \
      nsenter --net="/run/netns/${ue_netns[index]}" -- "${sionna_python}" "${bench_dir}/radio_traffic.py" dl-recv \
      --bundle "${traffic_bundle}" --ue "${ue_ids[index]}" --log "${log_dir}/rx-dl-${ue_ids[index]}.bin" \
      --until-file "${traffic_stop_file}"
  done
  start_group traffic-dl-send "${log_dir}/traffic-dl-send.log" "${aux_pin[@]}" "${sionna_python}" \
    "${bench_dir}/radio_traffic.py" dl-send --bundle "${traffic_bundle}" --log "${log_dir}/tx-dl.bin" \
    --epoch-file "${epoch_file}" --control-endpoint "${control_endpoint}"
  for index in "${!ue_ids[@]}"; do
    start_group "traffic-ul-send-${ue_ids[index]}" "${log_dir}/traffic-ul-send-${ue_ids[index]}.log" "${aux_pin[@]}" \
      nsenter --net="/run/netns/${ue_netns[index]}" -- "${sionna_python}" "${bench_dir}/radio_traffic.py" ul-send \
      --bundle "${traffic_bundle}" --ue "${ue_ids[index]}" --log "${log_dir}/tx-ul-${ue_ids[index]}.bin" \
      --epoch-file "${epoch_file}" --control-endpoint "${control_endpoint}"
  done
  for name in traffic-dl-send.log; do
    wait_log "${log_dir}/${name}" '"event": "radio_traffic_sender_ready"' "${started_pid}" 60 || true
  done
  sleep 2
  : >"${hold_file}"
  printf 'event=measure_release unix_ms=%s\n' "$(date -u +%s%3N)"
  replay_status="running"
  next_load_sample=0
  while true; do
    if grep -q '"event":"replay_measure_end"' "${log_dir}/replay-bridge.log" 2>/dev/null; then
      replay_status="measure_end"; measure_status="completed"; break
    fi
    if grep -q '"event":"replay_stalled"' "${log_dir}/replay-bridge.log" 2>/dev/null || ! process_running "${replay_pid}"; then
      replay_status="failed"; measure_status="replay_failed"; break
    fi
    if ! process_running "${broker_pid}" || ! process_running "${gnb_pid}"; then
      replay_status="interrupted"; measure_status="process_exited"; break
    fi
    if [[ "${SECONDS}" -ge "${next_load_sample}" ]]; then
      next_load_sample=$((SECONDS + 5))
      { printf '{"unix_ms": %s, "loadavg": "%s", "top": [' "$(date -u +%s%3N)" "$(cut -d' ' -f1-3 /proc/loadavg)"
        ps -eo pcpu=,comm= --sort=-pcpu | head -8 | awk '{printf "%s{\"cpu\": %s, \"comm\": \"%s\"}", (NR>1?", ":""), $1, $2}'
        printf ']}\n'; } >>"${log_dir}/host-load.jsonl"
    fi
    sleep 0.5
  done
  printf 'event=measure_end status=%s unix_ms=%s\n' "${measure_status}" "$(date -u +%s%3N)"
  sleep "${drain_seconds}"
  : >"${traffic_stop_file}"
  sleep 1
fi

# --- stop, then summarize -----------------------------------------------------
stop_named traffic || true
stop_named gnb-metrics-recorder || true
gnb_alive=0; process_running "${gnb_pid}" && gnb_alive=1
srsue_alive=1
for pid in "${srsue_pids[@]}"; do process_running "${pid}" || srsue_alive=0; done
stop_named replay || true
stop_named broker || true

/usr/bin/python3 - "${log_dir}" "${report_dir}/gate-summary.json" "${policy}" "${rrc[*]}" "${pdu[*]}" "${ping_ok[*]}" \
  "${ue_ids[*]}" "${gnb_alive}" "${srsue_alive}" "${measure_status}" "${replay_status}" <<'PY'
import json, pathlib, re, sys
(log_dir, out_path, policy, rrc, pdu, ping_ok, ue_ids, gnb_alive, srsue_alive, measure_status,
 replay_status) = sys.argv[1:]
log_dir = pathlib.Path(log_dir)
ids = ue_ids.split()


def text(name):
    path = log_dir / name
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def ue_events(ue):
    t = text(f"srsue-{ue}.log")
    pcis = re.findall(r"PCI=(\d+)", t + text(f"srsue-{ue}-internal.log"))
    return {"out_of_sync": t.count("detected out-of-sync"),
            "rrc_release": t.count("releasing RRC") + t.count("RRC Release"),
            "random_access_complete": t.count("Random Access Complete"),
            "crashed": int("srsRAN crashed" in t), "camped_pci": int(pcis[-1]) if pcis else -1}


stop = [line for line in text("broker.log").splitlines() if line.startswith("event=stop ")]
counters = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", stop[-1])} if stop else {}
broker = {k: counters.get(k, -1) for k in ("tx_pulls", "rx_requests", "rx_starvations", "tx_queue_overflows",
                                           "tx_sequence_gaps", "zmq_errors", "control_batches_committed")}
per_ue = {ue: {"rrc_connected": int(rrc.split()[i]), "pdu_session": int(pdu.split()[i]),
               "ping_ok": int(ping_ok.split()[i]), **ue_events(ue)} for i, ue in enumerate(ids)}
policy_check = json.loads(text("policy-check.json")) if text("policy-check.json") else None
attached = all(v["rrc_connected"] and v["pdu_session"] and v["ping_ok"] for v in per_ue.values())
clean = all(broker[k] == 0 for k in ("tx_queue_overflows", "tx_sequence_gaps", "zmq_errors"))
summary = {
    "policy": policy, "per_ue": per_ue, "broker": broker, "policy_check": policy_check,
    "measure_status": measure_status, "replay_status": replay_status,
    "gnb_alive_at_stop": int(gnb_alive), "srsue_alive_at_stop": int(srsue_alive),
    "all_ues_attached": attached, "broker_clean": clean,
    "status": "passed" if (attached and clean and measure_status == "completed" and (policy_check or {}).get("ok")
                           and int(gnb_alive) == 1 and int(srsue_alive) == 1
                           and not any(v["crashed"] for v in per_ue.values())) else "failed",
}
out = pathlib.Path(out_path)
out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"summary={out} status={summary['status']}")
raise SystemExit(0 if summary["status"] == "passed" else 1)
PY
