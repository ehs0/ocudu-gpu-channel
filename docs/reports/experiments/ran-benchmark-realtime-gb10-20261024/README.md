# ran_benchmark real time on the GB10 (2026-10-22 .. 10-26)

The single-cell scheduler benchmark (`use_cases/ran_benchmark`, lock-step
broker, OCUDU gNB, N srsUEs) ran at 0.41-0.57x real time on the DGX Spark
GB10 and fell to 0.40x at 8 UEs and 0.31x at 16. This report pins the causes
by measurement, applies local fixes, and records every session
([sessions.md](sessions.md)). Outcome, same fixture (sutd_1gnb, `mixed`,
pedestrian uniform, seed 20261016, 30 radio seconds after 10 s warm-up,
RR + HARQ-OLLA twice as an A/A pair), real-time ratio test 1 / test 2:

| UEs | Before (pinned srsUE) | NEON LDPC only | Full recipe | Gates |
| ---: | --- | --- | --- | --- |
| 1 | 0.88 (g7) | | 0.98 (SIMDe, g38) | PASS |
| 2 | 0.76 (g11) | | 0.98 (SIMDe, g48) | PASS |
| 4 | 0.56 / 0.57 (g14) | 0.993 / 0.995 (g58) | 0.998 / 0.998 (g70, g92, g97) | PASS |
| 8 | 0.40 / 0.39 (g15) | 0.78 / 0.78 (g59) | 0.93 / 0.94 (g85, g94, g98, g99, g100: 0.927-0.942) | PASS |
| 16 | 0.31 / 0.31 (g16) | 0.37 / 0.36 (g62) | 0.46 / 0.46 (g96) | RADIO ALIGNMENT misses one update |

1.0 is the ceiling: the broker paces the producers at real time
(`runtime.pacing`), so a slot that takes longer than 1 ms is lost and a
faster one gains nothing back.

## Setup

GB10 (10 big cores 5-9 and 15-19, 10 little cores 0-4 and 10-14), container
`ocudu-minwoo`, isolated native root with the pinned builds symlinked and its
own `builds/srsran4g-zmq-local` (srsRAN_4G `eea87b1` + the srsUE patch lock)
and `builds/ocudu-gpu-channel-single-cell` (broker at `c5eb514`). The gNB is
either the pinned CPU OCUDU `a1916edc` or the local build with
`integrations/ocudu/patches/ocudu-zmq-lower-phy-profile.patch`. 20 MHz, 15 kHz
SCS, 106 PRB, 1 antenna. Every session is the benchmark's own request through
`run-single-cell-scheduler-benchmark.sh`; the numbers are its
`run-report.json` `real_time_ratio` and `validation.json` gates. The
"before" rows already use the big-core placement below and `rx_sleep=false`;
the stock placement is worse (0.41 at 4 UEs, g1/g2).

## Cause 1: srsUE decodes PDSCH with the scalar LDPC decoder on aarch64

A per-slot trace inside srsUE (`SRSUE_SLOT_TRACE_DIR`, local instrumentation
not in this commit) split the UE's receive path: the full-band PDSCH decode
took 1.6-2.9 ms per 1 ms slot and 91 % of it was
`srsran_ldpc_decoder_decode_crc_c`, 571 us per 8448-bit code block. The
symbol was the generic `ldpc_dec_c` decoder: srsRAN_4G has AVX2 and AVX-512
LDPC decoders (`ldpc_dec_c_avx2*.c`, `ldpc_dec_c_avx512*.c`) compiled only
under `HAVE_AVX2`/`HAVE_AVX512`, and `sch_nr.c` falls back to
`SRSRAN_LDPC_DECODER_C` everywhere else. On an x86 development machine this
path is never taken. Micro-benchmark (`ldpc_dec_*_test -b1 -l384`, one
Cortex-X925 core, coded bits; [ldpc-bench-neon.txt](ldpc-bench-neon.txt)):

| Decoder | X925 | A725 |
| --- | ---: | ---: |
| scalar C (pinned) | 3.0 Mb/s | |
| AVX2 through SIMDe 0.7.2 / 0.8.4 (`-DSRSRAN_LDPC_SIMDE=ON`) | 10.4 Mb/s | 5.1 Mb/s |
| native NEON (`ldpc_dec_c_neon.c`) | 68 Mb/s (BG1), 90 Mb/s (BG2) | 24 Mb/s |

`integrations/srsran/patches/srsue-ldpc-neon.patch` (now in
`srsue-local-patches.lock.json`, applied by `build-srsue-local.sh`):

- `lib/src/phy/fec/ldpc/ldpc_dec_c_neon.c`: a port of `ldpc_dec_c_avx2long.c`
  to NEON intrinsics. Each lifted node is stored as Z bytes in 16-byte
  sub-nodes (one `int8x16_t` holds 16 LLRs), the circular shifts of the
  quasi-cyclic matrix are two `memcpy`s, the min-sum scaling is a Q0.8
  multiply; the layered schedule, saturation and CRC hooks are the AVX2
  decoder's.
- `SRSRAN_LDPC_DECODER_C_NEON` in `ldpc_decoder.h` (under `HAVE_NEON`),
  init/free/decode entries in `ldpc_decoder.c`, the source in the FEC
  `CMakeLists.txt`.
- `sch_nr.c` selects it by default on aarch64; `SRSRAN_LDPC_DECODER=c|simde|neon`
  overrides for A/B runs.
- `ldpc_dec_neon_test`: the AVX2 test's harness; decoded bits must equal the
  reference for BG1 and BG2 at lifting sizes 2, 3, 16, 24, 64, 112, 208 and
  384 (all pass).
- Optional, off: `-DSRSRAN_LDPC_SIMDE=ON` compiles the AVX2 encoder/decoder
  through SIMDe (`simde_shim/immintrin.h`, `SRSRAN_SIMDE_ROOT` for a
  checkout). It was the first fix (3.5x) and is kept for comparison.

Live effect at 8 UEs: full-band decode p50 0.30 ms, 28 us per code block,
0.09 cores per UE. Cell DL stays at 24-26 Mb/s with PRB utilisation 0.87,
and ue0's DL BLER drops from 2.0-2.7 % to 0.9-1.3 % (the late decodes were
also the HARQ failures).

## Cause 2: the gNB runs with zero lead

With the UE fast, the hop trace of the broker (`OCG_HOP_TRACE_DIR`) still
showed the gNB producing each DL block only after the matching UL block had
arrived. OCUDU's `lower_phy_baseband_processor::dl_process` waits while
`timestamp > last_rx_timestamp + rx_to_tx_max_delay`, and the stock bound is
1 ms: the DL may not run ahead of the last received UL block, so the gNB's
lead over the UE is 0 and any UE late by a few hundred microseconds costs a
whole slot. `ocudu-zmq-lower-phy-profile.patch` already exposes the bound as
`OCUDU_LPHY_RX_TO_TX_MAX_MS`; 3 ms gives two blocks of lead (the srsUE side
runs about two blocks ahead by itself). The benchmark can now select that
build with `SINGLE_CELL_GNB_BINARY`. Effect: 4 UEs 0.993 -> 0.998, 8 UEs
0.78 -> 0.81. The same patch's threaded lower-PHY profiles
(`OCUDU_ZMQ_LOWER_PHY_PROFILE=single|dual`, with or without
`OCUDU_ZMQ_RADIO_RT_PRIO`/`OCUDU_ZMQ_IO_RT_PRIO`) stall this gNB (460 ms
gaps, UEs do not attach; g64, g66, g73, g75, g78) and must stay unset.

## The rest at 8 UEs: logging, placement, broker wait policy

- **Logging.** At info level the gNB wrote 274 MB in 40 s (RLC 833k, PDCP
  446k, GTPU 355k, PHY 340k lines) and each srsUE 350 MB (PHY_LIB 2.3M
  lines), inline in the data paths. `SINGLE_CELL_QUIET_LOGS=1` puts gNB
  phy/rlc/pdcp/gtpu and srsUE phy_lib at warning (the SCHED lines the
  analysis parses stay) and `SINGLE_CELL_PCAP=off` drops the MAC/NGAP pcap:
  31 MB and 39 MB. 8 UEs 0.81 -> 0.82.
- **Placement.** The stock single-cell placement puts the broker on little
  core 13 and the UEs on 14-19 (little 14 among them). Broker GPU-call p99
  495 us on the little core against 135 us on a big one. Used: gNB 5-12
  (8 CPUs, so OCUDU's main pool min(5, cpus-3) gets its 5 workers), broker
  15-16, UEs 17-19 with two UEs per big core and the overflow (ue6, ue7 at
  8 UEs) on little cores 13-14 (`SINGLE_CELL_CPUS_UE<i>`), helpers 0-4.
  Every other split was worse: broker on one core without spin 0.54, three
  UEs per big core 0.71, four per core with a 4-core broker 0.50, broker on
  little cores 0.45, gNB on five big cores with main pool 5 (attach or PDU
  session fails on the first test, g69).
- **Broker wait policy.** The broker runs a puller and a REP worker per
  port plus a producer per node, 27 threads at 8 UEs, on two cores.
  `OCG_BROKER_SPIN=1` (every worker spins; the spark-gb10 profile's choice
  for the OAI gates) 0.81-0.82; the broker default (blocking waits)
  0.87-0.89 (g80, g89, g93); `OCG_BROKER_SPIN=2` (spin on in-process waits
  only, socket receives block) 0.93-0.94 in five sessions.
  `OCG_ZMQ_IO_THREADS=4` adds nothing over 2 (g94); the srsUE default
  `rx_sleep` loses 0.05 (g88, g95).

## What is left

- **8 UEs, 0.93.** The remaining 6 % are cycle tails (lock-step cycle p50
  1.0 ms, p90 1.6-1.7 ms, p99 2.4 ms); half of them come from the two UEs on
  little cores (turnaround p50 0.65 ms against 0.50 ms on a big core). The
  ten big cores are spoken for (gNB 5, broker 2, UEs 3). 1.0 at 8 UEs needs
  more big cores, not another knob.
- **16 UEs, 0.46.** The gNB's own per-slot turnaround is 0.76-1.0 ms (p50)
  and the cycle 2.0-2.2 ms: the CPU PHY/MAC is the ceiling, as the scaling
  proposal's first item says. `OCG_BROKER_SPIN=2` took it from 0.38-0.40 to
  0.46.
- Ruled out (no change or worse): `OCUDU_ZMQ_SINGLE_COPY=1`,
  `OCUDU_ZMQ_RADIO_RT_PRIO`/`OCUDU_ZMQ_IO_RT_PRIO` with the blocking profile,
  `OCUDU_ZMQ_RADIO_IDLE_US=5` (gNB CPU 170 % -> 90 %, same rt), two srsUE
  PHY workers, hop/slot traces off.

## Benchmark issues met on the way (for the maintainer)

- **A timed-out session kills the next one.** `controller._stop_locked`
  waits `CLEANUP_GRACE_S` (45 s) and then SIGKILLs every process whose
  environment has `SINGLE_CELL_SESSION_DIR=<root>/active`. The marker is the
  same for every session, so a session started within those 45 s dies about
  46 s after start with an empty `runner.log` and every log stopping at
  once. The marker should carry the session id.
- `channel_trace.py build` sometimes hangs at 0 % after
  `jitc_llvm_init(): LLVM API initialization failed` (3 of 9 sessions in
  one chain); there is no timeout around the trace build or the attach.
- `scene_registry` hard-codes `ue_counts` per scene (now 1, 2, 4, 8, 16 for
  sutd_1gnb and simple_blocks_2x2); the README's "N = 1, 8 or 16" and the
  profiles disagree.
- The gNB needs at least 8 pinned CPUs for its 5 main-pool workers; with 5
  it gets 2 and the first test's attach or PDU session fails.
- The controller's "runner exited without a final state" message now
  carries the runner's exit code.

## Reproduction

```bash
# srsUE with the lock (NEON decoder on aarch64)
OCUDU_NATIVE_ROOT=... scripts/native/build-srsue-local.sh
# gNB with the lower-PHY profile patch (for OCUDU_LPHY_RX_TO_TX_MAX_MS)
OCUDU_NATIVE_ROOT=... scripts/native/build-ocudu-gnb-local.sh
# one session (the recipe; everything else default)
SINGLE_CELL_CPUS_GNB=5-12 SINGLE_CELL_CPUS_BROKER=15-16 SINGLE_CELL_CPUS_UES=17-19 SINGLE_CELL_CPUS_AUX=0-4 \
SINGLE_CELL_UE_RX_SLEEP=false SINGLE_CELL_QUIET_LOGS=1 SINGLE_CELL_PCAP=off \
OCG_BROKER_SPIN=2 OCG_ZMQ_IO_THREADS=2 \
SINGLE_CELL_GNB_BINARY=$OCUDU_NATIVE_ROOT/builds/ocudu-zmq-local/apps/gnb/gnb OCUDU_LPHY_RX_TO_TX_MAX_MS=3 \
SINGLE_CELL_CPUS_UE0=17 SINGLE_CELL_CPUS_UE1=18 SINGLE_CELL_CPUS_UE2=19 SINGLE_CELL_CPUS_UE3=17 \
SINGLE_CELL_CPUS_UE4=18 SINGLE_CELL_CPUS_UE5=19 SINGLE_CELL_CPUS_UE6=13 SINGLE_CELL_CPUS_UE7=14 \
bash use_cases/ran_benchmark/run-single-cell-scheduler-benchmark.sh '{"ue_count":8,"gnb_antennas":1,"mobility":"pedestrian","placement":"uniform","traffic_profile":"mixed","tests":[{"scheduler":"rr","link_adaptation":"harq_olla"},{"scheduler":"rr","link_adaptation":"harq_olla"}],"seed":20261016,"measure_seconds":30,"warmup_seconds":10,"repeat_count":1,"comparison_order":"abba"}'
```

Each run's `logs/local-knobs.log` records the knobs; the broker's
`OCG_HOP_TRACE_DIR` gives the per-hop cycle split used above.

## Verification of the committed lock build

The sessions above ran a srsUE binary that also carried the slot-trace
instrumentation. The committed patch was rebuilt from the pinned tree with
`build-srsue-local.sh` (lock sha256 `4284baa2…`, srsue `0e32b703…`) and
re-run once with the recipe on 2026-10-26: 4 UEs 0.998 / 0.999 (g103), 8 UEs
0.943 / 0.948 (g104), all gates PASS, cell DL 23-24 Mb/s. See the end of
[sessions.md](sessions.md).

## Evidence

- [sessions.md](sessions.md): every tagged session, both runs' real-time
  ratio, gates, placement and knobs.
- [ldpc-bench-neon.txt](ldpc-bench-neon.txt): decoder correctness sweep and
  speeds on X925 and A725.
- Spark: `/workspace/ocudu-spark-ranb/results/single-cell-scheduler-benchmark/tagged-*`
  (reports, broker hop traces under `/workspace/gpuch/hop-<tag>/`, srsUE slot
  traces under `/workspace/gpuch/slottrace-<tag>/`).
