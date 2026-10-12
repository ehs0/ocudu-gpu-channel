#!/usr/bin/env bash
# V2X glue for the native multi-UE gate's OCUDU_NATIVE_MUE_UE_EXEC hook.
#
# The gate runs this inside each UE's network namespace right after that UE
# pinged the core. Only vehicle A's UE (index 0) runs the V2X client: it binds
# the UE's TUN address, so it can only receive what came over the 5G link, and
# relays each datagram to drive.py over a unix socket in V2X_RUN (the
# filesystem is shared with the stack namespace). The other UE is parked and
# carries no application traffic. ue_client.py is stdlib-only.
#
# Usage (as the gate expands it):
#   bash ue_exec.sh {ue_id} {ue_ip} {ue_index} {run_dir} {log_dir}
# Knobs: V2X_RUN (required), V2X_PORT (7100), V2X_UE_PYTHON (/usr/bin/python3).
set -euo pipefail
ue_id="$1"; ue_ip="$2"; ue_index="$3"; run_dir="$4"; log_dir="$5"
if [[ "${ue_index}" != 0 ]]; then
  printf '%s event=ue_exec_idle ue=%s reason=parked\n' "$(date -u +%s%3N)" "${ue_id}"
  exit 0
fi
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pkg_dir="$(dirname "${script_dir}")"
v2x_run="${V2X_RUN:?set V2X_RUN}"
mkdir -p "${log_dir}/v2x" "${v2x_run}"
exec "${V2X_UE_PYTHON:-/usr/bin/python3}" "${pkg_dir}/ue_client.py" \
  --bind "${ue_ip}:${V2X_PORT:-7100}" --relay-socket "${v2x_run}/a-relay.sock" \
  --log "${log_dir}/v2x/ue_rx.csv" --ready-file "${v2x_run}/ue-ready"
