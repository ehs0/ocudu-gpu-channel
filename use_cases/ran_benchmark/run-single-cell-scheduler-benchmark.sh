#!/usr/bin/env bash
# Single-cell scheduler benchmark from the command line: the same controller
# the web UI uses (validation, stop + clear of the previous session, frozen
# bundle, cold policy runs, fairness audit), without the web server.
#
#   bash use_cases/ran_benchmark/run-single-cell-scheduler-benchmark.sh \
#     '{"ue_count": 4, "mobility": "static", "placement": "uniform", "traffic_profile": "mixed",
#       "tests": [{"scheduler": "rr"}, {"scheduler": "qos"}], "seed": 1, "measure_seconds": 120, "warmup_seconds": 10}'
#
# Runs as root (namespaces, Open5GS TUN); needs the broker built once:
#   python3 use_cases/ran_benchmark/single_cell_runner.py preflight --ctest
# Environment: OCUDU_NATIVE_ROOT, OCUDU_NATIVE_SIONNA_PYTHON (scripts/native/env.sh);
# SB_ALLOW_UNVALIDATED=1 permits scene/UE-count combinations without evidence
# in scene_validation.json; SB_ARCHIVE=1 keeps replaced sessions under archive/.
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
# shellcheck source=../../scripts/native/env.sh
source "${repo_root}/scripts/native/env.sh"
[[ "$#" -le 1 ]] || { echo "usage: $0 ['<json request>']" >&2; exit 2; }
request="${1:-}"
[[ -n "${request}" ]] || request="{}"
declare -a extra=()
[[ "${SB_ALLOW_UNVALIDATED:-0}" == "1" ]] && extra+=(--allow-unvalidated)
[[ "${SB_ARCHIVE:-0}" == "1" ]] && extra+=(--archive)
exec /usr/bin/python3 "${script_dir}/single_cell_runner.py" cli \
  --sessions-root "${OCUDU_NATIVE_ROOT}/results/single-cell-scheduler-benchmark" \
  --native-root "${OCUDU_NATIVE_ROOT}" --sionna-python "${OCUDU_NATIVE_SIONNA_PYTHON}" \
  --config "${request}" "${extra[@]}"
