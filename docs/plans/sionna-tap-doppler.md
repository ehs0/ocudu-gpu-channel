# Per-tap Doppler between Sionna updates

Status: **implemented, opt-in**; offline validation done; **live V2X fails**: ue0 loses
slot sync with Doppler on (see [Live](#live-2026-10-14-gb10)). Date: 2026-10-14. Host for every number
below: DGX Spark GB10, 23.04 MS/s, V2X blind-intersection scenario (blocked
site), 1.8425 GHz DL / 1.7475 GHz UL.

## Problem

The Sionna bridge computes a Doppler shift for every path (`paths.doppler`,
from the node velocities), but `rays_to_taps()` dropped it: a tap carried
only delay, gain and phase. The emulator held each profile constant until
the next update (10 Hz) and then replaced it. At 13.9 m/s and 1.84 GHz the
largest Doppler is 85 Hz, so a tap should turn 8.5 times in 100 ms; instead
it stood still and then jumped to an effectively random phase.

Raising the update rate does not fix this (one update costs 51 ms p50,
74 ms p99), and the existing CFO step rotates a whole edge at one rate, which
cannot make taps fade against each other.

## Method

Each tap may carry `doppler_hz`. From the slot a profile takes effect, the
backend turns tap *k* at every sample:

    h_k(n) = |h_k| exp(j (phase_k + 2 pi f_D,k t(n)))

Sionna updates keep setting the path set, delays, gains and the phase
reference; the rotation in between is local to the backend and needs no
extra control messages.

### Time base

`t(n)` is defined per physical link (`TapRotationClock`, `physical_link.h`)
on the link sample clock that already times Jakes fading, so every lane of a
link turns from the same instant:

    t(n) = age + rate * (n - anchor) / fs

- **anchor**: the link sample index of the slot where the profile took effect.
- **Unstamped profile**: `age = 0`, `rate = 1`. Time is sample time from the
  first slot. Unit tests use this mode, and it stays deterministic.
- **Stamped profile** (`phase_time_unix_ns`): the phases describe a wall-clock
  instant, the instant whose positions were traced. `age` is wall time at the
  snap minus that instant. `rate` is wall seconds per sample second: the
  median, over the last 16 stamped anchors, of each interval's wall time over
  its sample time. The median keeps a stalled interval from setting the rate
  of the next update.

Both parts of the stamped mode exist because the measurement showed the
simple choice to be wrong:

| Source of mismatch at the next update (live run `20261014T052435Z`) | Size | Phase error at 85 Hz |
|---|---|---|
| Trace→ACK latency jitter between updates, if rotation starts at the apply slot | 3 ms p50, 8 ms p90 | 1.6 rad p50, 4.3 rad p90 |
| Emulated stream slower than wall time (UE producer 954 slots/s, 1 ms each) | 4.6 % of 100 ms | 2.5 rad |

The phase time removes the first; the rate removes the second. The rate is
the "continuous phase, next-interval f_D correction" boundary option applied
to the whole link rather than per tap: in the UE's sample time a tap turns
at `rate × f_D`, 4.8 % above Sionna's value at the measured 0.954x stream
rate. That is the rate the geometry actually advances in the UE's time. A
per-tap correction is impossible, because tap identity does not survive an
update (below).

### Rotation resolution

The rotation is evaluated per sample, not per slot. Holding a tap's phase
for one 1 ms slot at 85 Hz leaves an error ramp of 0.53 rad inside the slot.
That is −10 dB error energy for a slot-start phase and −16 dB for a mid-slot
phase, far above the bin-merging error of the chosen option (−41 dB p50). Per-sample
rotation costs one `sincosf` per tap and sample. The CUDA cost is measured
under [Timing](#timing).

### Paths merged into one tap (decision: option A, 1 Hz)

Paths in one 1/16-sample delay bin used to merge into one tap. With Doppler
on, a bin is split by `round(f_D / 1 Hz)`, and each tap carries the
power-weighted mean Doppler of its paths. Taps may share a delay. The
32-tap cap and the strongest-first selection are unchanged.

Measurement (`scripts/sionna_rt/measure_tap_doppler.py`). The positions and
velocities of the live run above were replayed through the same scene,
solver and binning, in two windows of 3,000 updates (bridge iterations 100
and 20,000). Both windows agree within 1 dB, and the figures below are for
`gnb0>ue0`; `ue0>gnb0` matches. The NMSE is energy-weighted over 100 ms
against the bin's true path sum.

| | Delay bins only (C, power-weighted mean) | Split at 20 Hz | Split at 5 Hz | **Split at 1 Hz (A)** |
|---|---|---|---|---|
| Taps per update, p50 / max | 14 / 20 | 15 / 21 | 15 / 23 | **17 / 24** |
| NMSE over 100 ms, p50 / p90 / p99 | −27.8 / −20.4 / −16.4 dB | −29.3 / −22.2 / −16.8 | −34.1 / −22.2 / −19.1 | **−41.3 / −29.7 / −22.3 dB** |
| Updates whose tap layout changes | 99.3 % | 99.3 % | 99.3 % | 99.4 % |
| Power dropped by the 32-tap cap | none | none | none | none |

60 % of the kept tap power lies in bins holding more than one path. Inside
those bins the Doppler spread is 0.1 Hz p50 but 6 Hz p90 and 24 Hz p99,
which is not small enough to justify C on its own. A costs three taps (p50)
and no extra history resets, because the layout already changes on 99 % of
moving updates. Option B (several components per tap) is A with the
components written as separate taps at the same delay, so it needs no
separate format.

### Update boundaries (decision: overwrite, timed by the phase time)

A new profile replaces the old one at its anchor. The old tap phases are
not carried over: tap identity does not survive an update. The delays move
0.055 samples per 100 ms (p50), and 1/16-sample bins put a new layout on
99 % of moving updates. A short crossfade would need both layouts convolved
at once, and spreading a 0.9 rad jump over 5 ms adds a spurious 29 Hz.

Measured boundary mismatch: the band frequency response (19.08 MHz) of the
previous profile rotated to the next update, against the next trace. Pairs
with a frozen feed are excluded (see Limits). Same replay, `gnb0>ue0`,
window 100:

| Boundary handling | NMSE p50 / p90 / p99 |
|---|---|
| Constant taps (before this work) | +3.0 / +5.9 / +8.3 dB |
| Tap Doppler, power-weighted per tap | −5.7 / −1.9 / +2.8 dB |
| … plus the predicted delay drift | −6.1 / −2.0 / +2.8 dB |

Per path, `phase + 2 pi f_D dt` predicts the next trace's phase to
0.15 rad p50, 0.67 rad p90 and 1.15 rad p99 (power-weighted). Without
rotation the error is 1.5 / 2.7 / 3.1 rad. The residual comes from what a
constant f_D cannot hold: the Doppler itself changes between updates
(1.4 Hz p50, 8.5 Hz p99, from acceleration and geometry), paths appear and
vanish, and gains change. The feed's own timestamps and the bridge clock
give the same prediction accuracy (0.147 vs 0.144 rad p50), so the feed is
not a source of error.

Live boundary mismatch is logged by the broker (`event=tap_rotation_anchor`
per stamped profile) and computed by `scripts/sionna_rt/analyze_tap_doppler_run.py`
under four time bases (static, apply slot, phase time, phase time + rate).
It has not been run yet (see Validation).

### Delay drift (decision: left to the next update)

A moving path's delay drifts 0.055 samples p50 and 0.106 samples p99 per
100 ms. Modelling the drift between updates (τ̇ = −f_D / f_c) improves the
boundary NMSE by only 0.3–0.7 dB (table above). Doing it would require a
fractional-delay filter that changes every sample. The drift stays a step at
the next update.

### UL / DL

Sionna traces each direction at its own carrier. The measured Doppler ratio
of the strongest path is 0.948437 against f_UL/f_DL = 0.948440, so each
direction's Sionna value is used as it is.

### Out of scope

The scene contains no moving reflectors (for example vehicle B), so all
Doppler here comes from ue0's own velocity. Moving objects in the Sionna
scene are separate work.

## Message format

Both fields are optional. Absent means 0, the behaviour before this work.

```json
{"type": "matrix_profile_swap", "link_id": "gnb0>ue0:sionna_rt", "nt": 1, "nr": 1,
 "phase_time_unix_ns": 1791955563159000000,
 "lanes": [{"rx_port": 0, "tx_port": 0,
            "taps": [{"delay_samples": 1.5, "gain_db": -12.0, "phase_rad": 0.25,
                      "doppler_hz": -61.3}]}]}
```

- `taps[].doppler_hz` (`profile_swap` and `matrix_profile_swap`): finite Hz,
  Sionna's sign convention, `a(t) = a exp(+j 2 pi f_D t)`.
- `phase_time_unix_ns`: a non-negative integer. JSON numbers are doubles,
  so present-day values resolve to 256 ns. The bridge and the broker must
  share a wall clock (same host today).
- A tap with Doppler 0 is omitted from the bridge's message, so with the
  feature off the bridge sends byte-for-byte the messages it sent before.
- History: a Doppler change counts as a gain/phase change and keeps history
  (`matrix_history_compatible`); a delay or layout change resets it, as
  before.

The bridge opts in with `--tap-doppler-bin-hz 1` (native gates:
`OCUDU_NATIVE_SIONNA_TAP_DOPPLER_BIN_HZ=1`). The phase time it sends is the
live feed sample's publish stamp, a grid point's due time, or the moment it
read the positions.

## Validation

Offline on GB10, all with the CUDA backend as the primary path and the CPU
backend as the reference.

- **Default unchanged**: `tests/channel_fingerprint.cpp` was built against
  HEAD (`10d2531`) and this tree. All six fixtures (static TDL, Jakes + LOS,
  2×2 fading, gain/phase-only matrix updates, moving-delay matrix updates and
  scalar profiles) give bit-identical output hashes on both CPU and CUDA.
  `rays_to_taps` with the feature off matched HEAD on 3,000 random path sets.
- **Analytic** (`tests/test_tap_doppler.cpp`, CTest `tap_doppler`):
  - One tap: worst sample error 5.0e-7 against `g exp(j(phi + 2 pi f t))`;
    fitted slope 85.000000 Hz on both backends.
  - Two taps at 85 / −40 Hz: worst sample error 7.9e-7; beat period
    7.99991 ms (CPU) and 7.99993 ms (CUDA) against 8 ms. The envelope minimum
    is flat, so the discrete argmin moves by up to 4 samples.
  - A Doppler/gain/phase update keeps history and re-anchors.
  - A stamped profile starts at `2 pi f age`.
  - Median rate ignores a 2 s stall.
  - CPU/CUDA parity with fractional delays: 2.7e-7.
- **Mutation**: each of these makes its test fail:
  - CUDA rotation off;
  - CPU rotation off;
  - no re-anchor;
  - phase time dropped at the snap;
  - mean instead of median rate;
  - Doppler counted as a layout change;
  - parser dropping `doppler_hz`.
- **Suites**: CTest 13/13. Python 227 passed, 6 skipped. Two failures in the
  uncommitted `tests/test_v2x_web.py` happen only when it runs after
  `test_sionna_web_ui.py`, because both import a module named `server`. They
  are unrelated to this change and pass alone.

### Timing

`tests/channel_timing_probe.cpp`: one 1×1 link, 1 ms slots (23,040 samples)
back to back, 300 consecutive `gnb0>ue0` profiles from the live run applied
at 10 Hz, 20,000 slots, two repeats. With Doppler on, the profiles are
option A rebuilt from the logged path points (14 → 17 taps p50).

| CUDA, GB10 | p50 | p99 | max | Calls > 1 ms | Warmups / 200 updates |
|---|---|---|---|---|---|
| HEAD build | 32.8–32.9 µs | 70.9–73.9 µs | 286–306 µs | 0 | 143 |
| This build, Doppler off | 32.9–33.7 µs | 70.1–71.7 µs | 304–314 µs | 0 | 143 |
| This build, Doppler on | 37.8 µs | 76.5–82.6 µs | 282–299 µs | 0 | 143 |

The CPU reference takes 4.1 ms off and 5.9 ms on per 1 ms slot. It is not a
real-time path in either mode.

### Live (2026-10-14, GB10)

`run-ocudu-v2x-intersection.sh`: blocked site, 40 dB, modes v2x / v2x:none /
no-v2x, 600 s each, same build. Broker processing comes from
`process_latency_summary`; nominal-slot misses come from broker telemetry.
The UE metrics are srsUE's `PDSCH` log lines.

| Run | Gate | ue0 re-attach | Broker p50/p99 (ue0) | DL nominal misses | UE out-of-sync / SFN slips | UE PDSCH decodes > 1 ms |
|---|---|---|---|---|---|---|
| Off `20261014T093806Z` | pass | 0 | 55 / 2,035 µs | (not sampled) | 0 / 0 | 12 % |
| Off `20261014T105756Z` | pass | 0 | 55 / 2,015 µs | 1.93 % | 0 / 0 | 13 % |
| On `20261014T102328Z` (held velocity) | fail | 20 | 60 / 2,045 µs | (not sampled) | 70 / 49,739 | 13 % |
| On `20261014T104052Z` (feed fix) | fail | 11 | 55 / 2,045 µs | 1.93 % | 40 / 50,196 | 19 %, p99 4.9 ms |

- **Emulator.** Doppler on and off cost the same in the live broker:
  processing p50/p99 and nominal-slot misses match.
  - The rate estimate held at 1.043 ± 0.005 (p10–p99).
  - The time-mapping error at boundaries was 0.74 ms p50 and 1.8 ms p90,
    which is 0.4–1.0 rad at 85 Hz.
  - Live boundary NMSE (`analyze_tap_doppler_run.py`): +3.8 dB p50 with
    constant taps, −0.1 dB with tap Doppler. Offline replay predicted
    −5.7 dB.
- **First failure: held velocity.** In the first on-run, the feed held
  ue0's last sample between episodes with its velocity still set, so the
  taps kept rotating.
  - Two taps at the same delay (−29.7 and −33.1 dB) had Doppler only
    0.08 Hz apart. A 1 Hz bin edge split them, and they drifted into
    cancellation: band power fell to −40 to −45 dB.
  - ue0 lost the DL, and SR failure released the RRC connection.
  - Fix: the bridge now traces a node that sent no new sample since the
    last update as standing still.
    `test_an_update_without_a_new_sample_traces_the_node_standing_still`
    fails without the fix.
- **Remaining failure: UE out-of-sync, cause not proven.** With the fix,
  ue0 held for episodes e000–e006 (DL SNR minimum 7.6–14 dB, as with
  Doppler off).
  - It then went out of sync in e007 and did not re-attach.
  - The emulated band power at that moment was normal (−19 dB, the same as
    in seconds where the UE reported 22–28 dB SNR).
  - Both on-runs show srsUE `SYNC: detected out-of-sync... skipping slot`
    followed by tens of thousands of `PBCH-MIB: SFN does not match`. Both
    off-runs show none.
  - Just before the first out-of-sync, PDSCH decoding took 1.36 ms
    (9 LDPC iterations). On the on-run, decode iterations were p90 10
    against 5 with Doppler off, and decode time was p99 4.9 ms against
    1.6 ms.
  - The time-varying channel makes srsUE work harder and lose slot
    timing. Whether that is srsUE's real-time budget on two pinned cores,
    its tracking of a rotating channel, or the 10 Hz boundary jumps is not
    separated yet.

**Diagnostic runs (2026-10-15).** Same setup, Doppler on, with bridge
switches that only these runs use (`--diagnostic-*`):

| Run | Change | Gate | ue0 out-of-sync / SFN slips | PDSCH > 1 ms, LDPC iter p90 |
|---|---|---|---|---|
| `20261015T030717Z` | Doppler scaled ×0.235 (max ~20 Hz) | pass | 0 / 0 | 12.5 %, 5 |
| `20261015T031831Z` | one moving profile (−59 / −59 / +77 Hz), then no updates, taps rotate 9 min | pass | 0 / 0 | 0.3 %, 2 |
| `20261015T032918Z` | UE on 4 big cores, gNB on 3 | gNB did not start in 15 s | — | — |

- Low Doppler with full updates and shadowing behaves like Doppler off.
- Rotation alone, up to 136 Hz between strong taps, does not break srsUE
  sync. That held profile is a line-of-sight one (strongest tap −13 dB),
  so this run has no shadowed, low-SNR stretch.
- Not yet separated: whether it is high Doppler with the 10 Hz update
  boundaries, or high Doppler with low SNR that drives srsUE out of sync.
  The UE CPU budget could not be tested without taking big cores from the
  gNB or the broker.

## Limits

- **Boundary residual.** Even with rotation, the boundary channel differs from
  the next trace by −5.7 dB p50 and −1.9 dB p90 (NMSE). Doppler drift within an
  interval, path births and deaths, and gain changes are not modelled between
  updates.
- **Frozen feed (fixed).** The feed held ue0's last sample between
  episodes, and with its velocity still set the phases were extrapolated
  over a geometry that no longer moved. The bridge now traces such a node
  as standing still (see Live).
- **Live link loss.** With Doppler on, srsUE ue0 loses slot sync
  mid-run. The cause is not separated yet (see Live).
- **History resets.** A history reset and warmup still happens on 99 % of
  moving updates, because delays change. This existing behaviour is
  unchanged by this work.
- **Timestamp resolution.** Feed timestamps have 1 ms resolution: ±0.5 ms,
  or ±0.27 rad at 85 Hz. The rate estimate follows a stream rate that
  varies 0.935–0.967 per second (p10–p99).
- **Wall clock.** The phase time assumes the bridge and the broker share a
  wall clock.
