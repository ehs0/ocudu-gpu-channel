#!/usr/bin/env bash
# Run the indoor room scenario on the native stack: one 4-antenna gNB, two
# srsUEs walking the room, Sionna RT driving the channel.
#
#   scripts/local/run-room-1gnb-2ue.sh
#
# The two-cell docker smoke cannot express this -- it starts two cells by
# construction -- and splitting two handsets across two cells is what kept
# leaving one of them unable to attach. One cell removes that: both UEs camp
# on the same gNB or neither does.
#
# The uplink and downlink calibration offsets are deliberately different. A
# single offset leaves one direction wrong in a room this small: at a level
# that keeps the downlink readable, every UE uplink arrives past the cell's
# full scale, PRACH survives on processing gain while msg3 clips into CRC
# failures, and the UE gets a random-access response it can never follow up.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

export OCUDU_NATIVE_ROOT="${OCUDU_NATIVE_ROOT:-${HOME}/ocudu-native-workspace}"
export OCUDU_NATIVE_SIONNA_PYTHON="${OCUDU_NATIVE_SIONNA_PYTHON:-${HOME}/OCUDU/venvs/sionna/bin/python}"
export OCUDU_NATIVE_SIONNA_SCENARIO="${OCUDU_NATIVE_SIONNA_SCENARIO:-${repo_root}/examples/sionna/room-table-1gnb-2ue.json}"
export OCUDU_NATIVE_SIONNA_UPDATE_HZ="${OCUDU_NATIVE_SIONNA_UPDATE_HZ:-10}"
# Open5GS writes its subscriber database into the locked ocudu checkout and
# docker-compose mounts it back in, so a checkout that has served a run is
# never byte-clean again. The commit and every tracked file still have to
# match; only files git has never seen are tolerated.
export OCUDU_NATIVE_ALLOW_UNTRACKED="${OCUDU_NATIVE_ALLOW_UNTRACKED:-1}"

# Measured against the reference street-canyon run, whose working links sit
# at a strongest tap of -13..-19 dB. The room is close enough to its cell
# that the same physical channel lands far hotter, hence the split.
bridge_args=(
  --gain-offset-db "${OCUDU_ROOM_DL_GAIN_DB:-32}"
  --uplink-gain-offset-db "${OCUDU_ROOM_UL_GAIN_DB:-12}"
  --max-depth "${OCUDU_ROOM_MAX_DEPTH:-4}"
  --samples-per-src "${OCUDU_ROOM_SAMPLES:-50000}"
)
export OCUDU_NATIVE_SIONNA_EXTRA_ARGS="${OCUDU_NATIVE_SIONNA_EXTRA_ARGS:-${bridge_args[*]}}"

[[ -x "${OCUDU_NATIVE_SIONNA_PYTHON}" ]] || {
  printf 'Sionna python is not executable: %s\n' "${OCUDU_NATIVE_SIONNA_PYTHON}" >&2
  exit 2
}
[[ -f "${OCUDU_NATIVE_SIONNA_SCENARIO}" ]] || {
  printf 'missing scenario: %s\n' "${OCUDU_NATIVE_SIONNA_SCENARIO}" >&2
  exit 2
}

printf 'scenario: %s\n' "${OCUDU_NATIVE_SIONNA_SCENARIO}"
printf 'bridge:   %s\n' "${OCUDU_NATIVE_SIONNA_EXTRA_ARGS}"
exec "${repo_root}/scripts/native/run-ocudu-sionna-multi-ue.sh" "$@"
