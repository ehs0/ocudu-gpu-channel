#!/usr/bin/env bash
# One cold policy run of the single-cell scheduler benchmark.
#
#   gnb0 (one policy) <-> ONE broker <-> ue0..ue{N-1} (netns ue1..ueN)
#
# The channel is not solved here: replay_bridge.py replays a frozen channel
# trace (channel_trace.py) at measurement sample S0 + k * samples_per_update
# of every link, and radio_traffic.py offers a frozen traffic trace on the same
# sample clock. single_cell_runner.py calls this once per policy execution
# with the SAME bundles; every process starts cold (core, broker, gNB, UEs).
#
# Usage (all paths absolute):
#   run-single-cell-policy.sh --run-root DIR --scenario FILE --channel-bundle DIR
#     --traffic-bundle DIR --policy rr|qos|maxci --ue-count N --profile NAME --seed S
#     [--link-adaptation fixed|amc|amc_olla|harq_olla] [--fixed-dl-mcs M --fixed-ul-mcs M]
#     [--awgn-snr-db DB|off] [--tx-power-ul P] [--tx-power-dl P] [--gnb-rx-gain DB]
#     [--attach-timeout S] [--drain-seconds S] [--warmup-seconds S] [--channel-build DIR]
# Environment: OCUDU_NATIVE_ROOT, OCUDU_NATIVE_SIONNA_PYTHON (scripts/native/env.sh).
# Results under --run-root: configs/ logs/ report/ (gate-summary.json, run-report.json).
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
native_dir="${repo_root}/scripts/native"
# shellcheck source=../../scripts/native/env.sh
source "${native_dir}/env.sh"
native_root="${OCUDU_NATIVE_ROOT}"
sionna_python="${OCUDU_NATIVE_SIONNA_PYTHON:-}"
inner="${script_dir}/run-single-cell-policy-inner.sh"

usage_error() { printf 'error: %s\n' "$1" >&2; exit 2; }

run_root="" scenario="" channel_bundle="" traffic_bundle="" policy="" ue_count="" profile="" seed=""
link_adaptation="amc_olla" fixed_dl_mcs="" fixed_ul_mcs=""
awgn="40" tx_power_ul="" tx_power_dl="" gnb_rx_gain="" attach_timeout="240" drain_seconds="6" warmup_seconds="0"
channel_build="${OCUDU_NATIVE_CHANNEL_BUILD:-${native_root}/builds/ocudu-gpu-channel-single-cell}"
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --run-root) run_root="${2:-}"; shift 2 ;;
    --scenario) scenario="${2:-}"; shift 2 ;;
    --channel-bundle) channel_bundle="${2:-}"; shift 2 ;;
    --traffic-bundle) traffic_bundle="${2:-}"; shift 2 ;;
    --policy) policy="${2:-}"; shift 2 ;;
    --link-adaptation) link_adaptation="${2:-}"; shift 2 ;;
    --fixed-dl-mcs) fixed_dl_mcs="${2:-}"; shift 2 ;;
    --fixed-ul-mcs) fixed_ul_mcs="${2:-}"; shift 2 ;;
    --ue-count) ue_count="${2:-}"; shift 2 ;;
    --profile) profile="${2:-}"; shift 2 ;;
    --seed) seed="${2:-}"; shift 2 ;;
    --awgn-snr-db) awgn="${2:-}"; shift 2 ;;
    --tx-power-ul) tx_power_ul="${2:-}"; shift 2 ;;
    --tx-power-dl) tx_power_dl="${2:-}"; shift 2 ;;
    --gnb-rx-gain) gnb_rx_gain="${2:-}"; shift 2 ;;
    --attach-timeout) attach_timeout="${2:-}"; shift 2 ;;
    --drain-seconds) drain_seconds="${2:-}"; shift 2 ;;
    --warmup-seconds) warmup_seconds="${2:-}"; shift 2 ;;
    --channel-build) channel_build="${2:-}"; shift 2 ;;
    *) usage_error "unexpected argument: $1" ;;
  esac
done

[[ -x "${sionna_python}" ]] || usage_error "OCUDU_NATIVE_SIONNA_PYTHON must point at the Sionna interpreter (pyzmq)"
[[ "${run_root}" == /* && ! -e "${run_root}" ]] || usage_error "--run-root must be an absolute path that does not exist"
for path in "${scenario}" "${channel_bundle}" "${traffic_bundle}"; do
  [[ "${path}" == /* && -e "${path}" ]] || usage_error "missing input: ${path}"
done
[[ "${policy}" =~ ^(rr|qos|maxci)$ ]] || usage_error "--policy must be rr, qos or maxci"
[[ "${link_adaptation}" =~ ^(fixed|amc|amc_olla|harq_olla)$ ]] || usage_error "--link-adaptation must be fixed, amc, amc_olla or harq_olla"
if [[ "${link_adaptation}" == "fixed" ]]; then
  for mcs in "${fixed_dl_mcs}" "${fixed_ul_mcs}"; do
    [[ "${mcs}" =~ ^([0-9]|1[0-9]|2[0-8])$ ]] || usage_error "--link-adaptation fixed needs --fixed-dl-mcs and --fixed-ul-mcs 0..28"
  done
else
  [[ -z "${fixed_dl_mcs}${fixed_ul_mcs}" ]] || usage_error "--fixed-dl-mcs / --fixed-ul-mcs apply only to --link-adaptation fixed"
fi
declare -a la_args=(--link-adaptation "${link_adaptation}")
[[ "${link_adaptation}" != "fixed" ]] || la_args+=(--fixed-dl-mcs "${fixed_dl_mcs}" --fixed-ul-mcs "${fixed_ul_mcs}")
[[ "${ue_count}" =~ ^([1-9]|1[0-6])$ ]] || usage_error "--ue-count must be 1..16"
[[ "${profile}" =~ ^[a-z_]+$ ]] || usage_error "invalid --profile"
[[ "${seed}" =~ ^(0|[1-9][0-9]{0,8})$ ]] || usage_error "--seed must be a non-negative integer"
number_re='^-?[0-9]+([.][0-9]+)?([eE][-+]?[0-9]+)?$'
[[ "${awgn}" == "off" || "${awgn}" =~ ${number_re} ]] || usage_error "--awgn-snr-db must be a number or off"
[[ -z "${tx_power_ul}" || "${tx_power_ul}" =~ ${number_re} ]] || usage_error "invalid --tx-power-ul"
[[ -z "${tx_power_dl}" || "${tx_power_dl}" =~ ${number_re} ]] || usage_error "invalid --tx-power-dl"
[[ -z "${gnb_rx_gain}" || "${gnb_rx_gain}" =~ ^(0|-[0-9]+([.][0-9]+)?)$ ]] || usage_error "--gnb-rx-gain must be <= 0"
[[ "${attach_timeout}" =~ ^[1-9][0-9]*$ && "${attach_timeout}" -ge 30 ]] || usage_error "--attach-timeout >= 30"
[[ "${drain_seconds}" =~ ^[1-9][0-9]?$ ]] || usage_error "--drain-seconds must be 1..99"
[[ "${warmup_seconds}" =~ ^[0-9]+$ ]] || usage_error "--warmup-seconds must be a non-negative integer"

# SINGLE_CELL_GNB_BINARY selects another gNB build (e.g. builds/ocudu-zmq-local, the lower-PHY profile patch
# whose OCUDU_LPHY_RX_TO_TX_MAX_MS lets the DL run ahead of the last UL block); default: the pinned build.
gnb_binary="${SINGLE_CELL_GNB_BINARY:-${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb}"
broker="${channel_build}/ocudu-gpu-channel"
for path in "${inner}" "${gnb_binary}" "${broker}" "${native_root}/builds/open5gs-v2.7.6/tests/app/5gc" \
  "${native_root}/install/mongodb-6.0.29/bin/mongod" "${script_dir}/replay_bridge.py" "${script_dir}/radio_traffic.py" \
  "${script_dir}/render_single_cell.py" "${script_dir}/single_cell_analyze.py"; do
  [[ -e "${path}" ]] || usage_error "missing required path: ${path} (build the broker once: single_cell_runner.py --preflight)"
done
for command_name in unshare nsenter ip mount umount flock setsid stdbuf taskset; do
  command -v "${command_name}" >/dev/null 2>&1 || usage_error "missing command: ${command_name}"
done
[[ -c /dev/net/tun ]] || usage_error "/dev/net/tun is absent"
[[ "$(git -C "${native_root}/src/ocudu" rev-parse HEAD)" == "a1916edcdbcd70ba6e0af47ee87be061dad5a4e4" ]] || \
  usage_error "OCUDU revision mismatch"

# One single-cell policy run at a time on this host (radios, GPU and CPU
# placement are shared). The lock file is outside every run directory.
lock_file="${native_root}/run/single-cell-scheduler-benchmark.lock"
mkdir -p "$(dirname "${lock_file}")"
exec {lock_fd}>"${lock_file}"
flock -n "${lock_fd}" || usage_error "another single-cell policy run holds ${lock_file}"
export OCUDU_NATIVE_GATE_LOCK_FD="${lock_fd}"

config_dir="${run_root}/configs" log_dir="${run_root}/logs" report_dir="${run_root}/report"
data_dir="${run_root}/data" run_dir="${run_root}/run"
mkdir -p "${config_dir}" "${log_dir}" "${report_dir}" "${data_dir}" "${run_dir}/netns"

declare -a render_args=(--repo-root "${repo_root}" --native-root "${native_root}" --output-dir "${config_dir}"
  --log-dir "${log_dir}" --scenario-config "${scenario}" --policy "${policy}" --ue-count "${ue_count}"
  --traffic-profile "${profile}" --seed "${seed}" "${la_args[@]}")
if [[ "${awgn}" != "off" ]]; then
  render_args+=(--awgn-snr-db "${awgn}")
  [[ -z "${tx_power_ul}" ]] || render_args+=(--tx-power-ul "${tx_power_ul}")
  [[ -z "${tx_power_dl}" ]] || render_args+=(--tx-power-dl "${tx_power_dl}")
fi
/usr/bin/python3 "${script_dir}/render_single_cell.py" "${render_args[@]}" >"${log_dir}/render.log" 2>&1 || {
  cat "${log_dir}/render.log" >&2; usage_error "config rendering failed"; }
if [[ -n "${gnb_rx_gain}" ]]; then
  grep -qx '  rx_gain: 0' "${config_dir}/gnb0.yaml" || usage_error "gnb0.yaml has no 'rx_gain: 0' line"
  sed -i "s/^  rx_gain: 0\$/  rx_gain: ${gnb_rx_gain}/" "${config_dir}/gnb0.yaml"
fi
# --- optional knobs (all off by default; the GB10 real-time recipe is in
# docs/reports/experiments/ran-benchmark-realtime-gb10-20261024/README.md) --------
# SINGLE_CELL_UE_RX_SLEEP=false    -> srsUE ZMQ rx_sleep=false (srsue-zmq-rx-sleep.patch; the broker paces)
# SINGLE_CELL_LOG_LEVEL=warning    -> gNB all_level + srsUE all_level
# SINGLE_CELL_PCAP=off             -> gNB MAC/NGAP pcap off
# SINGLE_CELL_QUIET_LOGS=1         -> gNB phy/rlc/pdcp/gtpu and srsUE phy_lib at warning, the rest at info
# SINGLE_CELL_UE_PHY_THREADS=n     -> srsUE [phy] nof_phy_threads (default 3)
# SINGLE_CELL_GNB_MAIN_POOL=n      -> gNB expert_execution.threads.main_pool.nof_threads (OCUDU default min(5, cpus-3))
# SINGLE_CELL_GNB_MAIN_POOL_CPUS / SINGLE_CELL_GNB_RU_CPUS -> gNB main pool / RU affinity masks (RU_CPUS untested)
if [[ "${SINGLE_CELL_UE_RX_SLEEP:-true}" == "false" ]]; then
  sed -i '/^device_args *=/ s/$/,rx_sleep=false/' "${config_dir}"/srsue-ue*.conf
fi
if [[ -n "${SINGLE_CELL_LOG_LEVEL:-}" ]]; then
  sed -i "s/^all_level = info$/all_level = ${SINGLE_CELL_LOG_LEVEL}/" "${config_dir}"/srsue-ue*.conf
  sed -i "s/^  all_level: info$/  all_level: ${SINGLE_CELL_LOG_LEVEL}/" "${config_dir}/gnb0.yaml"
fi
if [[ "${SINGLE_CELL_PCAP:-on}" == "off" ]]; then
  sed -i "s/^  mac_enable: enable$/  mac_enable: false/; s/^  ngap_enable: enable$/  ngap_enable: false/" "${config_dir}/gnb0.yaml"
fi
if [[ "${SINGLE_CELL_QUIET_LOGS:-0}" == "1" ]]; then
  # drop the per-PDU / per-candidate info logging that runs inline in the
  # gNB (RLC/PDCP/GTPU/PHY) and srsUE (PHY_LIB) data paths; SCHED (via mac) and the UE PHY/RRC lines the
  # analysis and gates parse stay at info.
  sed -i "s/^  all_level: info$/  all_level: info\n  phy_level: warning\n  rlc_level: warning\n  pdcp_level: warning\n  gtpu_level: warning/" "${config_dir}/gnb0.yaml"
  sed -i "s/^all_level = info$/all_level = info\nphy_lib_level = warning/" "${config_dir}"/srsue-ue*.conf
fi
if [[ -n "${SINGLE_CELL_UE_PHY_THREADS:-}" ]]; then
  for f in "${config_dir}"/srsue-ue*.conf; do printf '\n[phy]\nnof_phy_threads = %s\n' "${SINGLE_CELL_UE_PHY_THREADS}" >>"$f"; done
fi
if [[ -n "${SINGLE_CELL_GNB_MAIN_POOL:-}" || -n "${SINGLE_CELL_GNB_MAIN_POOL_CPUS:-}" || -n "${SINGLE_CELL_GNB_RU_CPUS:-}" ]]; then
  grep -q '^expert_execution:' "${config_dir}/gnb0.yaml" && usage_error "gnb0.yaml already has expert_execution"
  { printf 'expert_execution:\n'
    [[ -n "${SINGLE_CELL_GNB_MAIN_POOL_CPUS:-}" ]] && printf '  affinities:\n    main_pool_cpus: %s\n    main_pool_pinning: mask\n' "${SINGLE_CELL_GNB_MAIN_POOL_CPUS}"
    [[ -n "${SINGLE_CELL_GNB_MAIN_POOL:-}" ]] && printf '  threads:\n    main_pool:\n      nof_threads: %s\n' "${SINGLE_CELL_GNB_MAIN_POOL}"
    [[ -n "${SINGLE_CELL_GNB_RU_CPUS:-}" ]] && printf '  cell_affinities:\n    - ru_cpus: %s\n      ru_pinning: mask\n' "${SINGLE_CELL_GNB_RU_CPUS}"
    true; } >>"${config_dir}/gnb0.yaml"
fi
printf 'event=local_knobs ue_rx_sleep=%s log_level=%s pcap=%s broker_env=%s ue_phy_threads=%s gnb_main_pool=%s quiet_logs=%s gnb_binary=%s rx_to_tx_max_ms=%s\n' "${SINGLE_CELL_UE_RX_SLEEP:-true}" \
  "${SINGLE_CELL_LOG_LEVEL:-default}" "${SINGLE_CELL_PCAP:-on}" "${OCG_BROKER_SPIN:-unset}" "${SINGLE_CELL_UE_PHY_THREADS:-3}" "${SINGLE_CELL_GNB_MAIN_POOL:-auto}" "${SINGLE_CELL_QUIET_LOGS:-0}" \
  "${gnb_binary}" "${OCUDU_LPHY_RX_TO_TX_MAX_MS:-unset}" | tee "${log_dir}/local-knobs.log"
"${gnb_binary}" -c "${config_dir}/gnb0.yaml" --dryrun >"${log_dir}/gnb0-dryrun.log" 2>&1 || {
  tail -20 "${log_dir}/gnb0-dryrun.log" >&2; usage_error "gnb0 dry run failed"; }

# CPU placement on spark-gb10 (big X925 5-9,15-19; little A725 0-4,10-14).
# Up to 8 UEs: the gNB sees eight CPUs (OCUDU needs >= 5: its worker pool is
# min(needed, CPUs - 3)), the broker one big core, the UEs share six, and
# helpers (core, replay, traffic, recorders) the little cores 0-4. Above 8
# UEs srsUE decoding dominates: the gNB keeps the five big cores 5-9, the
# broker (one producer thread per node) gets 10-11, the UEs share 2-4 and
# 12-19, and the helpers 0-1.
# shellcheck source=../../scripts/native/oai-gate-defaults.sh
source "${native_dir}/oai-gate-defaults.sh"
oai_gate_platform || usage_error "platform profile resolution failed"
if [[ "${OCUDU_NATIVE_PLATFORM_PROFILE:-none}" == "spark-gb10" && "${ue_count}" -gt 8 ]]; then
  cpus_gnb="5-9" cpus_broker="10-11" cpus_ues="2-4,12-19" cpus_aux="0-1"
elif [[ "${OCUDU_NATIVE_PLATFORM_PROFILE:-none}" == "spark-gb10" ]]; then
  cpus_gnb="5-12" cpus_broker="13" cpus_ues="14-19" cpus_aux="0-4"
else
  cpus_gnb="" cpus_broker="" cpus_ues="" cpus_aux=""
fi
# Placement experiments: SINGLE_CELL_CPUS_{GNB,BROKER,UES,AUX} override one role.
cpus_gnb="${SINGLE_CELL_CPUS_GNB:-${cpus_gnb}}" cpus_broker="${SINGLE_CELL_CPUS_BROKER:-${cpus_broker}}"
cpus_ues="${SINGLE_CELL_CPUS_UES:-${cpus_ues}}" cpus_aux="${SINGLE_CELL_CPUS_AUX:-${cpus_aux}}"

/usr/bin/python3 - "${report_dir}/run-parameters.json" "${policy}" "${ue_count}" "${profile}" "${seed}" "${awgn}" \
  "${tx_power_ul}" "${tx_power_dl}" "${gnb_rx_gain}" "${scenario}" "${channel_bundle}" "${traffic_bundle}" \
  "${OCUDU_NATIVE_PLATFORM_PROFILE:-none}" "${cpus_gnb}" "${cpus_broker}" "${cpus_ues}" "${cpus_aux}" \
  "${channel_build}" "${config_dir}/benchmark.json" <<'PY'
import json, sys
(out, policy, ue_count, profile, seed, awgn, tx_ul, tx_dl, rx_gain, scenario, channel, traffic, platform,
 gnb, broker, ues, aux, build, rendered) = sys.argv[1:]
json.dump({"policy": policy, "link_adaptation": json.load(open(rendered))["link_adaptation"],
           "ue_count": int(ue_count), "traffic_profile": profile, "seed": int(seed),
           "awgn_snr_db": None if awgn == "off" else float(awgn), "tx_power_ul": float(tx_ul) if tx_ul else None,
           "tx_power_dl": float(tx_dl) if tx_dl else None, "gnb_rx_gain_db": float(rx_gain) if rx_gain else None,
           "scenario": scenario, "channel_bundle": channel, "traffic_bundle": traffic, "platform_profile": platform,
           "cpus": {"gnb": gnb, "broker": broker, "ues": ues, "aux": aux}, "channel_build": build},
          open(out, "w"), indent=2, sort_keys=True)
PY

set +e
# --kill-child=TERM: if this script is stopped, the inner gets SIGTERM and runs its
# bounded cleanup instead of dying with its process groups still running.
unshare --user --map-root-user --net --mount --fork --kill-child=TERM --propagation private \
  "${inner}" --repo-root "${repo_root}" --native-root "${native_root}" --run-root "${run_root}" \
  --parent-netns "$(readlink /proc/self/ns/net)" --parent-mntns "$(readlink /proc/self/ns/mnt)" --outer-uid "$(id -u)" \
  --gnb-binary "${gnb_binary}" --broker "${broker}" --ue-count "${ue_count}" --policy "${policy}" \
  "${la_args[@]}" \
  --channel-bundle "${channel_bundle}" --traffic-bundle "${traffic_bundle}" --sionna-python "${sionna_python}" \
  --attach-timeout "${attach_timeout}" --drain-seconds "${drain_seconds}" \
  --cpus-gnb "${cpus_gnb}" --cpus-broker "${cpus_broker}" --cpus-ues "${cpus_ues}" --cpus-aux "${cpus_aux}"
inner_status="$?"
set -e

"${sionna_python}" "${script_dir}/single_cell_analyze.py" --run-root "${run_root}" \
  --channel-bundle "${channel_bundle}" --traffic-bundle "${traffic_bundle}" --warmup-seconds "${warmup_seconds}" \
  --out "${report_dir}/run-report.json" >"${log_dir}/analyze.log" 2>&1 || \
  echo "event=analyze_failed log=${log_dir}/analyze.log" >&2
printf 'event=single_cell_policy_run policy=%s link_adaptation=%s ues=%s status=%s run_root="%s"\n' \
  "${policy}" "${link_adaptation}" "${ue_count}" "${inner_status}" "${run_root}"
exit "${inner_status}"
