#!/usr/bin/env bash
# Rescue glue for the native multi-UE gate's OCUDU_NATIVE_MUE_UE_EXEC hook.
#
# The gate runs this inside each UE's network namespace right after that UE
# pinged the core. UE index i < RESCUE_ROBOTS carries robot_{i+1}: its UE link
# (ue_link.py, stdlib only) binds the UE's TUN address, so the only route to
# the mission server is the 5G user plane (the namespace has no other
# interface). robot.py reaches the link over a unix socket in RESCUE_RUN (the
# filesystem is shared with the stack namespace). Other UEs are parked.
#
# The link runs for the whole gate, across episodes; its offline queue is a
# file in RESCUE_RUN, so it also survives a restart of the link process.
#
# Usage (as the gate expands it):
#   bash ue_exec.sh {ue_id} {ue_ip} {ue_index} {run_dir} {log_dir}
# Knobs: RESCUE_RUN (required), RESCUE_ROBOTS (3), RESCUE_SERVER (10.45.1.1:7200),
# RESCUE_UE_PORT (7300), RESCUE_UE_PYTHON (/usr/bin/python3), RESCUE_LINK_EXTRA.
set -euo pipefail
ue_id="$1"; ue_ip="$2"; ue_index="$3"; run_dir="$4"; log_dir="$5"
robots="${RESCUE_ROBOTS:-3}"
if [[ "${ue_index}" -ge "${robots}" ]]; then
  printf '%s event=ue_exec_idle ue=%s reason=parked\n' "$(date -u +%s%3N)" "${ue_id}"
  exit 0
fi
robot="robot_$((ue_index + 1))"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pkg_dir="$(dirname "${script_dir}")"
rescue_run="${RESCUE_RUN:?set RESCUE_RUN}"
mkdir -p "${log_dir}/rescue/links/${robot}" "${rescue_run}"
# shellcheck disable=SC2086
exec "${RESCUE_UE_PYTHON:-/usr/bin/python3}" "${pkg_dir}/ue_link.py" --robot-id "${robot}" \
  --bind "${ue_ip}:${RESCUE_UE_PORT:-7300}" --server "${RESCUE_SERVER:-10.45.1.1:7200}" \
  --bus-socket "${rescue_run}/link-${robot}.sock" --queue-db "${rescue_run}/queue-${robot}.db" \
  --log-dir "${log_dir}/rescue/links/${robot}" --status-json "${rescue_run}/link-${robot}.json" \
  --ready-file "${rescue_run}/link-${robot}.ready" --iface tun_srsue ${RESCUE_LINK_EXTRA:-}
