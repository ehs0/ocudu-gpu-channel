# scheduler_benchmark — OCUDU MAC schedulers under one channel

Two modes. The **single-cell mode** (§ "Single-cell mode" below) runs one
gNB with N UEs and executes the policies one after another on a frozen
channel and traffic trace, with a web UI that starts, stops and replaces
sessions. The **two-cell mode** (this section) is the original side-by-side
gate and is unchanged.

Native gate: `bash examples/scheduler_benchmark/run-ocudu-scheduler-benchmark.sh`
(from the repo root). It sources `scripts/native/env.sh` and reuses the
native renderers there; results land under `$OCUDU_NATIVE_ROOT`.

## Run the scheduler benchmark (two cells × two UEs, one channel)

`run-ocudu-scheduler-benchmark.sh` compares two OCUDU MAC scheduling
policies under the same radio conditions. Cell a (`gnb0` PCI 1 with `ue0`,
`ue1`) and cell b (`gnb1` PCI 2 with `ue2`, `ue3`) run side by side on two
broker processes. **One** Sionna bridge solves a seeded scenario whose radios
are `gnb0`, `ue0`, `ue1` and sends every batch to both brokers
(`run_bridge.py --fanout-control-endpoint`, cell b under the rename
`gnb0→gnb1, ue0→ue2, ue1→ue3`), so `ue2` carries exactly `ue0`'s channel and
`ue3` exactly `ue1`'s, at the same instant. The cells differ only in
`cell_cfg.scheduler.policy`: `rr` (`rr_sched`, `scheduler_time_rr.cpp`) or
`qos` (`qos_sched`, `scheduler_time_qos.cpp`, OCUDU's default). The policy is
bound at gNB start-up and the remote-control commands cannot change it, which
is why the two policies run in parallel rather than in alternating periods.

**Reproducibility.** `OCUDU_NATIVE_SB_SEED` fixes the UE random-waypoint
routes (`examples/scheduler_benchmark/make_scenario.py`, kept in line of sight
of the gNB against the scene's obstacle boxes), the PathSolver seed and every
packet schedule. The bridge runs `--timeline grid`: update k is solved at
scenario time k / update_hz, never at "elapsed wall time", and scenario time
0 is held (`--hold-until-file`) until all four UEs have attached. Within a
run both cells receive the same solved batch, so their channels are equal by
construction. Across runs with the same seed (live runs on the GB10,
2026-10-01: ten run pairs, seven seed 1 and two seed 2 runs, 701-1322 shared
grid points per pair) the UE positions and the tap structure (count, delays)
were identical at every grid point, and 89-98% of the batches were
bit-identical (SHA-256 per batch, `--profile-digest`); at the rest the GPU's
floating-point summation order differed, by at most 0.0038 dB in tap gain
and 0.0005 rad in phase.
`examples/scheduler_benchmark/campaign.py` recomputes this for any set of
runs. The radio stack itself (thread timing, HARQ, when a channel update
lands relative to the IQ stream) is not deterministic; that is what the
per-second pairing and the A/A control below measure.

**Sequence.** Core, two brokers, the bridge (scenario time 0 applied), two
gNBs; the gate then checks each gNB's non-default configuration dump
(`log.config_level: info`) for the requested policy and refuses to continue
on a mismatch (`policy-check.json`). Four srsUEs attach; then at one instant
the seeded UDP traffic (`examples/scheduler_benchmark/traffic.py`) starts and
the bridge's grid point 0 is released. After `OCUDU_NATIVE_SB_MEASURE_SECONDS`
and a drain, `examples/scheduler_benchmark/analyze.py` writes
`results/reports/ocudu-scheduler-benchmark/<ts>/benchmark-report.json`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OCUDU_NATIVE_SB_SEED` | `1` | Scenario, solver and traffic seed. |
| `OCUDU_NATIVE_SB_SCHED_A` / `_B` | `rr` / `qos` | Policy of cell a / b. Run each seed twice with the policies swapped to cancel cell bias, and once with the same policy in both cells (A/A) to measure it. |
| `OCUDU_NATIVE_SB_PROFILE` | `mixed` | Traffic profile (`definitions.py`): `mixed` = UE A backlogged download, 5QI 9; UE B on/off video and 100 Hz control, 5QI 7. `full_buffer` = both backlogged, 5QI 9. `light` = Poisson well below capacity. The per-UE 5QI goes into the subscriber CSV's `qci` column. |
| `OCUDU_NATIVE_SB_TRAFFIC_OVERRIDES` | unset | `ue0.dl_bulk.rate_mbps=25,ue1.five_qi.value=9`, … |
| `OCUDU_NATIVE_SB_MEASURE_SECONDS` | `120` | Scenario seconds measured; the report pairs them second by second. |
| `OCUDU_NATIVE_SB_CPUS_*` | profile | On `spark-gb10`: both gNBs share 5-12, the four UEs share 15-19, brokers 13 / 14, everything else 0-4. A gNB must see at least five CPUs: OCUDU sizes its main worker pool as min(needed, CPUs − 3), and with one worker it stalls at MAC cell activation. |
| `OCUDU_NATIVE_SB_WEB_UI` / `_WEB_PORT` | `1` / `8090` | The benchmark UI (`examples/scheduler_benchmark/server.py --follow-latest`). |

**Metrics.** Every metric is computed per valid second (all four UEs in
service: present in their gNB's scheduler report with DL HARQ not failing
outright, and, when srsUE has written its metrics CSV, camped on its own PCI)
and reported per cell with a paired a − b difference and a batch-means 95%
CI. Application layer (UDP, one host clock): aggregate and per-UE
throughput, Jain's index, mean and p95/p99 one-way delay, PDB violation
rate against the UE's 5QI, drop rate. MAC layer (OCUDU `Slot decisions`
log, `lib/scheduler/log_reference.md`): PRB utilization, PRB shares,
spectral efficiency, DL wait while data is queued (`dl_bo > 0`) as the
starvation measure, and the scheduler's per-slot decision time. GBR
satisfaction is not measured: srsUE has one default non-GBR QoS flow.

**Radio speed.** The report and the UI state each cell's slots per second
(real time is 1000 at 15 kHz SCS). On the GB10 with this placement eight
`mixed` runs (2026-10-01) ran at 393-654 slots/s, varying from run to run
with no other busy process in the host-load samples (cause not identified);
within a run the two cells stayed within 4% of each other, so the comparison
stays paired, but wall-clock capacities and delays scale with this factor
and absolute numbers from different runs are not comparable.

**Cell bias is real; use swap pairs.** In the seed-1 A/A run (`rr` in both
cells, 20261001T083522Z) UE A's DL p99 delay was 244 ms higher in cell a
(95% CI 132-357 ms) and the aggregate DL 0.84 Mb/s lower (CI 0.40-1.28),
with the same policy, channel and traffic. A single run's paired CI therefore
does not separate the policy from the cell; the swap pair does.

**Known failure.** srsUE (srsRAN 4G 23.04 fork) can abort in RLC AM NR
(`rlc_am_nr.cc:1776`, `SO_start > SO_end` on a NACK segment) under the
backlogged 5QI 9 flow; one of eight runs lost UE A of cell a this way after
60 s. The lock-step broker then stalls the whole cell, the analyzer marks
those seconds invalid, and the gate fails on the crash (`per_ue.*.crashed`,
`srsue_alive_at_stop`).

In this container: `sudo env SB_SEED=1 SB_MEASURE=120 bash
/home/dev/ocudu-setup/run-isolated-scheduler-benchmark.sh`, or
`SB_CAMPAIGN="1:rr:qos 1:qos:rr 1:rr:rr"` for a swap pair plus an A/A run;
`examples/scheduler_benchmark/campaign.py <report dirs>` turns those into
cell-bias-free policy effects.

## Single-cell mode

One OCUDU gNB, one broker, N srsUEs (`ue0`..`ue{N-1}`, N = 1, 8 or 16), and the policies
(`rr`, `qos`) executed **sequentially** on the same cell, each run a cold start
of the whole stack (core, broker, gNB, UEs). The policy is bound at gNB
start-up, so a cold restart is the only way to change it; running the
policies on one cell removes the two-cell mode's cell bias by construction.

### Identical external conditions

The channel is solved **once** per session and replayed byte for byte; the
offered traffic is written down once and replayed on the radio clock.

| Input | How it is frozen | Where it is applied |
| --- | --- | --- |
| Channel | `channel_trace.py build` evaluates the scenario at scenario time k / update_hz (attach record + K updates) and stores each update as the exact canonical JSON `matrix_profile_swap` request per link, with SHA-256 per request, per batch and for the file. | `replay_bridge.py` commits update k of every link with `take_effect_at_sample = S0 + k * samples_per_update`. The broker splits its serve call at that sample, so the update starts on exactly that sample of the link's IQ stream, and logs `event=control_sample_applied` (tag with the request's hash, actual and target sample, digest of the profile it decoded). |
| Receiver noise | The broker's CUDA AWGN is Philox(seed, sample counter). | The k = 0 commit carries `noise_epoch`: every receiver restarts its counter at S0, so the noise drawn at measurement sample n is the same draw in every run (`event=noise_epoch`). Bit-exact restart: `tests/test_sample_indexed_control.cpp`. |
| Offered traffic | `traffic_trace.py` writes every packet (radio offset, flow, seq, size, UE) once; flow schedules are keyed by the UE's global index, so adding a UE leaves the others' packets unchanged. | `radio_traffic.py` releases a packet when its link's broker clock (`link_clock`) reaches S0 + round(t * fs) and logs target and observed sample. |

S0 is chosen when the measurement is released, after every UE has attached
(the attach phase runs on the trace's attach record and is not measured), so
it differs between runs; what is equal is everything **relative to S0**.
Segment k of every report is radio second k: the same 1000 slots, the same
10 channel updates and the same offered packets in every run, however fast
the run's radios ran. Outcomes (allocations, HARQ, throughput, delays, the
transmitted IQ) are allowed to differ, also between two runs of one policy.

### Fairness audit

`validation.py` writes `reports/fairness-audit.json` from recorded evidence
only (bundle re-hash, each run's broker log, gate summary):

| Gate | Passes when |
| --- | --- |
| TRACE VERIFIED | both bundles re-hash to their manifests; every run replayed them; for every link and k the applied request is the trace's and decoded to the same digest in every run (attach record likewise) |
| RADIO ALIGNMENT VERIFIED | every link applied every k exactly once, in order, on exactly sample S0 + k·period; every receiver restarted its noise at S0; no broker evidence line was unparseable |
| TRAFFIC VERIFIED | every offered packet was sent, with the same (flow, seq, target − S0) set in every run; injection lag p99 ≤ 3 slots and max ≤ 10 slots (the broker clock advances one ~1 ms serve call at a time) |
| RUN VALID | every UE attached, no srsUE crash, the replay reached the end of the measurement, broker transport clean, ≥ 90 % of radio seconds valid |

`STRICT FAIR COMPARISON: PASS` requires all four; otherwise the session is
`UNQUALIFIED` and the reasons are listed. A deliberately corrupted, dropped,
shifted or foreign update, a changed traffic set or excessive lag fails its
gate (`tests/test_single_cell_benchmark.py::FairnessAudit`).

### Web UI

```bash
# once: build the broker into builds/ocudu-gpu-channel-single-cell (+ ctest)
sudo bash /home/dev/ocudu-setup/run-isolated-single-cell.sh /usr/bin/python3 \
  examples/scheduler_benchmark/single_cell_runner.py preflight --ctest
# the dashboard on http://127.0.0.1:8092 (container wrapper: isolated mounts, root)
sudo bash /home/dev/ocudu-setup/run-isolated-single-cell.sh /home/dev/venvs/sionna/bin/python \
  examples/scheduler_benchmark/server.py --control --port 8092 [--archive] [--allow-unvalidated]
```

The page compares **Test 1** and **Test 2**. The shared settings (UE count,
scene, UE type, placement, traffic profile, seed, durations, run order and
runs per test) apply to both; each test panel then picks its own MAC
scheduler. A test is the shared configuration plus its own fields
(`definitions.TEST_FIELDS`, today only `scheduler`), so another comparison
dimension is added by naming its field there and applying it where the
policy run is built (`single_cell_runner.Session.run_test`); the panels draw
one selector per field from `/api/benchmark/options`. Choosing the same
scheduler on both sides is allowed and is reported as an A/A control. Runs
are named `NN-test1` / `NN-test2`, the order ABBA by default (Test 1, Test 2,
Test 2, Test 1), and the comparison is Test 1 − Test 2 per radio second.
Press **Start New Benchmark** to apply the settings. Selector changes
are pending until then. Start validates first (an invalid request leaves the
running session untouched), then stops the previous session's processes,
deletes `<sessions-root>/active` and nothing else (or moves it to
`archive/` with `--archive`), and starts a new session with a new id and a
higher generation number. The page clears itself before the request returns
and drops every response tagged with another generation. **Stop** stops the
session (STOPPED); **Reset** stops and clears it. A server restart adopts a
still-running session. The API is listed in `server.py`; it binds
127.0.0.1, needs `--token` on any other address, and refuses POSTs without
`X-Benchmark-Client: ui` or with a foreign Origin.

Only (scene, UE count) combinations with end-to-end evidence in
`scene_validation.json` are startable; `--allow-unvalidated` lifts that to
produce the evidence.

### Scene, UE type and placement

Two scenes are offered (`scene_registry.py`), each with ONE gNB and UE counts
1, 2 and 4:

| Scene | Geometry | gNB |
| --- | --- | --- |
| `sutd_1gnb` | SUTD campus (OpenStreetMap buildings, roads, grass and water; ITU concrete, glass, medium dry ground, wet ground) | Building 2 rooftop, 30.5 m |
| `simple_blocks_2x2` | Four 20 m square blocks around a 20 m crossroads (`scripts/sionna_rt/build_blocks_scene.py`): NE concrete 30 m, NW glass 22 m, SW wood 9 m, SE concrete 15 m | 25 m mast at the crossroads |

The robot_ring arena and the two-gNB SUTD layout remain in the repository
for the two-cell mode and other gates but are not offered here.

| Choice | Options |
| --- | --- |
| UE type | `static` (no movement) · `pedestrian` (ping-pong random-waypoint walk at 1.4 m/s) |
| UE placement | `uniform` (every allowed point equally likely) · `hotspot` (2 seeded centres 25-65 m from the gNB; UE i on centre i mod 2, Gaussian σ = 8 m per axis; a pedestrian walks around its own centre) |

"Allowed" means 15-80 m from the gNB, 3 m clear of every building footprint
and in 3D line of sight of the gNB above every OSM building prism (the
placement rule, recorded in the scenario). Each UE draws from its own seeded
stream and the hotspot centres from another, so adding UEs never moves the
existing ones. The 3D view draws the frozen trace's Sionna scene with each
object in the preview colour of its ITU radio material, a legend with the
relative permittivity and conductivity Sionna uses at the DL carrier (UL in
the tooltip), the hotspot discs, the UEs (hue + marker shape: octahedra for
ue8-ue15, dashed lines in the charts, since the categorical palette has eight
hues) and the OpenStreetMap credit.

### CLI

```bash
sudo bash /home/dev/ocudu-setup/run-isolated-single-cell.sh bash \
  examples/scheduler_benchmark/run-single-cell-scheduler-benchmark.sh \
  '{"ue_count": 4, "mobility": "pedestrian", "placement": "hotspot", "traffic_profile": "mixed",
    "tests": [{"scheduler": "rr"}, {"scheduler": "qos"}],
    "seed": 1, "measure_seconds": 120, "warmup_seconds": 10, "repeat_count": 2, "comparison_order": "abba"}'
```

Exit status 0 means `STRICT FAIR COMPARISON: PASS`. One policy run alone:
`run-single-cell-policy.sh --run-root DIR --scenario ... --channel-bundle ...
--traffic-bundle ... --policy rr --ue-count 4 --profile mixed --seed 1`.

### Session layout

```text
<native-root>/results/single-cell-scheduler-benchmark/
  active/  session.json status.json events.jsonl bundle-frozen.json
           bundle/{scenario.json,noise.json,channel/,traffic/}   read-only after freezing
           policy_runs/NN-<policy>/{configs,logs,report}/       one cold run each
           reports/{fairness-audit,benchmark-report,validation}.json, per-ue-kpis.csv
           runtime/  runner and per-run console logs
  archive/<session_id>/   only with --archive; never shown as current data
```

### Traffic profiles for N UEs

`full_buffer`: every UE bulk (5QI 9), DL 80/N Mb/s and UL 2 Mb/s each, so the
cell is oversubscribed at every N (5 Mb/s per UE at 16). `mixed`: even UE index bulk (5QI 9, DL
40/n_bulk Mb/s + 2 Mb/s UL), odd index interactive (5QI 7, on/off 12 Mb/s
video and 100 Hz DL/UL control). Rates are per radio second. One UE is a
control run; there is nothing to share, so fairness is not meaningful.

### Placement on the GB10 for 16 UEs

Above 8 UEs the gNB keeps the five big cores 5-9, the broker gets 10-11, the
UEs share 2-4 and 12-19 and the helpers 0-1 (up to 8 UEs: gNB 5-12, broker
13, UEs 14-19, helpers 0-4). UE i pins contention preamble 4i (distinct for
16 UEs). The attach budget is 120 + 15 N seconds.

All UEs reach the same PRACH occasion (the lock-step broker holds radio time
until every UE is connected, so starting them one by one does not separate
them). The gNB occasionally misses one of the preambles; the pinned srsUE
then stopped its RAR timer on an RAR without its RAPID or one it could not
unpack (`Error parsing RAR PDU`) and never sent another preamble, so the
session lost that UE before the measurement (sutd_1gnb, 4 UEs, 2026-10-17:
g15-g17). `scripts/native/patches/srsue-rar-window.patch` makes it retry.

### Radio calibration

Receiver floor 40 dB under a unit-gain link (`awgn_snr_db`), the UE scale the
legacy gate runs with (`tx_power_ul` 300), and gNB ZMQ `rx_gain: -55` so
srsUE's +42 dB wire level does not saturate the gNB's int16 input. All three
are recorded in `session.json`.

### Measured results

The sessions below ran before the scene set was narrowed to `sutd_1gnb`
(robot_ring and a 2-UE SUTD run with seeded-waypoint mobility); they remain
the record of the method. The `sutd_1gnb` sessions are listed after them.

GB10 (aarch64, CUDA broker, `builds/ocudu-gpu-channel-single-cell`), 23.04
MS/s, 15 kHz SCS, 106 PRB, 1 gNB + N srsUEs, Sionna RT trace (robot_ring,
seeded LOS waypoints, 10 Hz updates) → broker TDL per link + per-receiver
AWGN, `mixed` profile, 60 radio seconds measured after 5 s warm-up, seed 1:

| Session | N | Policies | Verdict | Updates on target | Radio speed | Cell DL (Mb/s) RR / QoS-PF | Jain DL RR / QoS-PF |
| --- | ---: | --- | --- | --- | --- | --- | --- |
| 20261017T054207Z-g6 | 2 | RR, QoS-PF | STRICT PASS | 2600/2600 per run | 0.49-0.51× | 36.03 / 34.88 | 0.770 / 0.779 |
| 20261017T050410Z-g2 | 4 | RR, QoS-PF | STRICT PASS | 5200/5200 per run | 0.41-0.42× | 37.09 / 34.00 | 0.985 / 0.996 |
| 20261017T055712Z-g9 | 4 | RR, QoS-PF, ABBA × 2 | STRICT PASS | 5200/5200 per run | 0.41-0.43× | 36.96 / 34.91 | 0.988 / 0.995 |
| 20261017T053436Z-g5 (SUTD campus) | 2 | RR, QoS-PF | STRICT PASS | 2600/2600 per run | 0.39-0.40× | 33.94 / 32.91 | 0.785 / 0.794 |

Jain is over the per-UE means (in `mixed` the interactive UEs' on/off load
caps their share, hence ~0.78 at N = 2). In the ABBA session, paired per
radio second (RR − QoS-PF, each policy averaged over its two runs): aggregate
DL +2.04 Mb/s (batch-means 95 % CI 1.19..2.89), per-second Jain −0.020
(CI −0.026..−0.015). The A/A control of the same session (same policy, same
frozen inputs, two cold runs) differs by −1.65 Mb/s (QoS-PF, CI −2.57..−0.73)
and −1.34 Mb/s (RR, CI −2.88..0.20) in aggregate DL, the later run higher in
both policies: run-to-run nondeterminism of the radio stack, plus an order
drift that ABBA cancels in the paired mean, is as large as the throughput
difference between the policies. The throughput difference is therefore not
established by one seed; the Jain difference exceeds the A/A spread
(0.005..0.014) only narrowly. Every interval covers the seconds of these runs,
not seed-to-seed variation; a campaign needs several seeds.

Current scenes (the evidence in `scene_validation.json`), `mixed` profile,
60 radio seconds after the warm-up, RR then QoS-PF:

| Session | Scene | N | UEs | Seed | Verdict | Radio speed | Cell DL (Mb/s) RR / QoS-PF | Jain DL RR / QoS-PF |
| --- | --- | ---: | --- | ---: | --- | --- | --- | --- |
| 20261017T085732Z-g11 | sutd_1gnb | 1 | pedestrian, uniform | 1 | STRICT PASS | 0.41-0.42× | 32.16 / 32.20 | — (control run) |
| 20261017T092854Z-g14 | sutd_1gnb | 2 | static, hotspot | 1 | STRICT PASS | 0.72-0.73× | 48.21 / 48.22 | 0.697 / 0.697 |
| 20261019T084447Z-g22 | sutd_1gnb | 4 | static, uniform | 3 | STRICT PASS | 0.41-0.43× | 53.85 / 54.55 | 0.861 / 0.859 |
| 20261019T085252Z-g23 | simple_blocks_2x2 | 4 | static, uniform | 20261016 | STRICT PASS | 0.28-0.47× | 52.21 / 55.07 | 0.849 / 0.830 |

g22 and g23 ran with `srsue-rar-window.patch`; every UE attached on its
first preamble.

**Deep in-band notches take a UE out of service.** sutd_1gnb, 4 static
uniform UEs, seed 20261016 (session 20261019T080609Z-g21) was UNQUALIFIED:
ue1 attached on the flat attach channel, but its multipath DL has a notch
~16 dB below the rest of the band at −4..−2 MHz from the carrier, where the
SSB sits. srsUE then reported RSRP ~7 and DL SNR −4..−7 dB (BLER ~90 %)
while ue3, 6 m away with the same wideband gain and a 5 dB dip, ran at
SNR ~15 dB. The gNB released ue1 in the RR run. The pedestrian 4-UE
failures of 2026-10-17 (g18: ue0 and ue2 out of service mid-walk) fit the
same cause. Placements are not screened for this; a seed whose static UEs
have dips of ≤ 9 dB (g22, g23) runs.

### Limitations

- Real time is not met: the radios ran at 0.41-0.48× (393-654 slots/s in the
  two-cell mode). Radio-time results are comparable across runs; wall-clock
  delays and rates scale with the speed and are reported separately.
- Packets are paced on the broker's clock, which advances per serve call
  (~1 ms); the gNB sees them through its IP stack, so injection is
  slot-granular, not sample-exact (lag p99 ≈ 1.85 ms in the session above).
- S0 is aligned to the broker's sample stream; the gNB's SFN at S0 differs
  between runs.
- The CPU reference backend's noise is not counter-indexed and reports the
  epoch as unsupported (`applied=0`), which fails the alignment gate.
- srsUE's RLC AM abort (above) still applies; a crash fails RUN VALID.
