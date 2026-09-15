# Why the Sionna multi-gNB gate could not keep two UEs attached

`ocudu-multi-gnb-smoke.sh` on the SUTD overlap scene had been failing with
`ue_stack_blocker_no_attach`: ue0 reached RRC and a PDU session but its ping
never landed, and ue1 never reached RRC at all. This records what the failure
actually was, measured rather than inferred.

There turned out to be **two independent defects plus one harness bug**. The
first is confirmed and fixed. The second was bounded and is now attributed: the
uplink "SINR ceiling" is the gNB's own DM-RS channel-estimate residual, not a
property of the link — and the experiment that was read as refuting the
clipping hypothesis turned out not to have applied the setting it reported.
Both are worked out in the two sections after Defect 2, added 2026-09-15.

**Host**: RTX 5090 (32 GB), driver 595.71.05, CUDA 13.2, kernel 6.17.0-35,
Ubuntu 24.04.
**Date**: 2026-09-14.
**Branch**: `miso-siso-sionna-rt` at `9b1cc73` plus uncommitted work.
**Stack**: two OCUDU gNBs + Open5GS + two srsRAN_4G srsUE containers, driven
through the CUDA broker with the Sionna RT bridge supplying every channel.

## Runs

| Run | Config | Result | ue0 | ue1 |
|---|---|---|---|---|
| `20260913T122349Z` | baseline, crosstalk present | `no_attach` | 1/1/**0** | **0/0/0** |
| `20260914T121458Z` | crosstalk removed, 60 s, no keepalive | **`passed`** | 1/1/1 | 1/1/1 |
| `20260914T133439Z` | crosstalk removed, 300 s hold, keepalive 1 s | `ping_failed` | 1/1/1 | 1/1/**0** |
| `20260914T142546Z` | as above + `--wire-capture` | `ping_failed` | 1/1/1 | 1/1/**0** |
| `20260914T144228Z` | as above + `UE_TX_GAIN=0` | **`passed`** | 1/1/1 | 1/1/1 |

Columns are `rrc_connected / pdu_session_established / ping_ok`.
`rx_starvations` fell from 10771 to 382/36/14 once the crosstalk edges were
gone; `tx_queue_overflows`, `tx_sequence_gaps` and `zmq_errors` were 0 in every
run.

Cell assignment was as designed in every passing run — ue0 on gnb0, ue1 on
gnb1, confirmed by matching each srsUE's reported C-RNTI against the two gNB
logs. The warning in the gate script about both UEs landing on gnb0 no longer
applies after `5f4c1de` moved gnb1's mast.

## Defect 1 (confirmed, fixed): UE-to-UE crosstalk summed with no duplex isolation

Both UEs receive the sum of every incoming edge, and the scene declares
`ue0 -> ue1` and `ue1 -> ue0` crosstalk edges. Measured from the baseline run's
`sionna-status.jsonl` over the first 120 s:

| | ue1 | ue0 |
|---|---|---|
| serving tap, median | −11.2 dB | −12.2 dB |
| crosstalk tap, peak | **−6.9 dB** | −6.9 dB |
| crosstalk louder than own serving cell | **14 % of updates** | 5 % |
| SIR incl. crosstalk (median / p10 / min) | 3.8 / −3.7 / −9.0 dB | 21.8 / 2.6 / −2.1 dB |
| SIR excl. crosstalk (median / p10 / min) | 4.1 / 0.6 / −0.6 dB | **88.0** / 33.5 / 13.3 dB |

The crosstalk peak sits **4.3 dB above ue1's own serving cell**, returning
roughly every 18 s as the two routes approach. That is what kept ue1 from
decoding its own cell's SSB and PDCCH.

It is also non-physical. Band n3 is FDD — TS 38.101-1 Table 5.2-1 gives UL
1710–1785 MHz and DL 1805–1880 MHz, duplex spacing 95 MHz — so a UE's uplink
emission cannot reach another UE's downlink demodulator as a waveform, only as
a raised noise floor. TS 38.101-1 Table 6.5.3.2-1 bounds it: for NR band
`n3, n80` protecting E-UTRA Band 3, the limit over `FDL_low – FDL_high` is
**−50 dBm / 1 MHz**. Against a power-class-3 +23 dBm carrier integrated over a
20 MHz victim channel that is **60 dB** of transmitter spectral isolation,
before any propagation loss. The emulator was applying none of it.

Note the bridge normalises: `--gain-offset-db` defaults to **60**, so reported
tap gains are absolute path gains shifted up by 60 dB (verified in the logs —
`power_db −68.617` appears as `gain_db −8.617`). The offset is global, so
relative comparisons above are unaffected, but no per-edge isolation can be
expressed through it. A `path_loss` step on the crosstalk model is the right
instrument: `matrix_profile_swap` replaces only taps and fading, so static
chain steps survive the swap.

Scene-specific MCL, for anyone re-deriving the number: the two UEs come no
closer than **25.8 m** over the route, where the traced coupling is −68.0 dB
absolute. Free-space at 25.8 m and 1747.5 MHz is 65.5 dB, so the ray tracing
is 2.5 dB above free space — consistent for isotropic antennas with
reflections. Against this scene's noise floor, break-even (crosstalk equal to
thermal noise at the worst peak) is 26 dB of isolation and the verdict is
insensitive above ~46 dB, so the 60 dB spec figure has ample margin.

**Removing the two edges let ue1 reach RRC and a PDU session in every
subsequent run.** Diagnostic variants used for the experiment:

- `examples/topology.sionna-multi-gnb-noxtalk.cuda.yaml` (10 links → 8, `crosstalk` model dropped)
- `examples/sionna/sutd/2gnb-2ue-overlap-noxtalk.json` (10 links → 8)

Everything else — masts, routes, solver, seed — is identical to the parents, so
the verdict difference is attributable to this edge alone. Zero crosstalk links
is already a supported configuration (the 1gnb-1ue layout has none), and the
gate derives its expected telemetry link list from the topology it was handed.

For the permanent fix, `path_loss_db: 60` on the `crosstalk` model keeps the
edge visible in the dashboard and contributes ~0.002 dB of noise-floor rise;
deleting the edge is the infinite-isolation limit and also drops one Sionna
trace group. Either is defensible; the graded version is live-tunable, so the
failure can be reproduced on demand by sweeping it back to 0.

## Defect 2 (open): sustained uplink collapses, and the cause is still unidentified

With crosstalk gone and a 1 s keepalive forcing continuous uplink activity,
**both** UEs collapsed the same way:

```
RRC Connected -> PDU session -> reconfiguration OK
  -> "Scheduling request failed: releasing RRC connection..."
  -> PRACH retries -> Msg3 fails (UL HARQ max reTxs) -> no recovery
```

The gNBs never sent `rrcRelease`. The UEs gave up locally after hitting
`sr-TransMax`. Run `20260914T121458Z` passed only because it had no keepalive:
the UEs went idle, were released by the 120 s inactivity timer, and **nothing
ever asked for an uplink grant**. That run is a valid attach result and not a
valid sustained-uplink result.

Measured uplink, run `20260914T133439Z`:

| | gnb0 (ue0) | gnb1 (ue1) |
|---|---|---|
| SR detected (`sr=yes`) | 39, mean **+13.6 dB** | 13, mean **+0.7 dB** |
| PUSCH `crc=OK` | 79, +4.0…14.9 (med 11.1) | 28, +9.1…15.0 (med 10.8) |
| PUSCH `crc=KO` | 14, −9.9…+3.2 | 15, −9.5…−5.0 |
| PUCCH, all occasions | median −18.9 dB | median −19.0 dB |

The ~10 200 `sr=no` occasions per cell at −19 dB are empty SR resources the
scheduler polls regardless, not failures. What matters is the **+8…15 dB
ceiling** on everything that does decode.

`--wire-capture` (1 s per port, skipping 15 s, run `20260914T142546Z`) shows
why that ceiling is not thermal:

| Port | TX peak amplitude | TX mean power | RX peak amplitude |
|---|---|---|---|
| `gnb0_p0` | 0.45 | 3.40e-04 | **212.4** (0.70 % of samples > 1.0) |
| `gnb1_p0` | 0.38 | 3.25e-04 | 0.00 (ue1 silent) |
| `ue0_p0` | **313.1** | 1.99e+02 | 0.24 |
| `ue1_p0` | 0.00 | 0 | 0.09 |

- The UE transmits **+56.8 dB** hotter in amplitude (+67.3 dB in burst power)
  than the gNB.
- The uplink signal therefore sits about **110 dB above** this topology's
  `noise_power` of 1.05e-7. **The uplink is not noise-limited, and the
  uncalibrated uplink noise floor is not what limits it.**
- gnb0's receiver is fed peak amplitude **212** — 212× full scale — and the
  over-scale fraction (0.70 %) matches ue0's transmit duty cycle exactly, so
  every uplink burst arrives over scale.
- The gNB's measured mean TX power, 3.40e-04, is within 2.6 dB of the
  `P_tx = 1.87e-4` recorded in `topology.sionna-multi-gnb.cuda.yaml`, which
  cross-validates both the method and that the documented figure is a mean.

One asymmetry is structural: `write_srsue_config` hardcodes `tx_gain = 50` and
srsUE's ZMQ RF driver applies it as a sample scale, while the OCUDU side
refuses anything above 0 dB (`Channel gain must be <= 0.0 dB for ZMQ-device`)
and `gen_gnb_config` forces it to 0. The bridge's 60 dB `gain_offset_db`
removes the scene's real path loss on top of that, leaving the uplink almost
lossless. The full scale budget is worked out in "The 56.8 dB TX scale gap"
below: it is entirely accounted for and contains no unexplained term.

### The clipping hypothesis was tested, but not by the test that was run

The obvious reading of the table above is that the gNB's receiver clips and
that the ~15 dB ceiling is a distortion floor. `OCUDU_MGNB_UE_TX_GAIN` was
added to test it (**default 50, so every proven gate stays byte-for-byte
unchanged**) and run `20260914T144228Z` set it to 0.

**That run did not apply 0 dB.** srsRAN_4G's `radio::init` gates the config
value on `args.tx_gain > 0` (`lib/src/radio/radio.cc:162`) and falls through to
`set_tx_gain(args.rx_gain)` otherwise, and `write_srsue_config` hardcodes
`rx_gain = 40`. So the run applied **40 dB**, not 0. The srsUE log proves it —
`20260914T144228Z/srsue0.log` carries `Warning: TX gain was not set. Using
open-loop power control (not working properly)`, the console line from that
else-branch, and `20260914T142546Z/srsue0.log` does not. The arithmetic closes
exactly: the UE's own baseband peak is 313.1 / 10^(50/20) = **0.990**, and
0.990 × 10^(40/20) = **99.01** against a measured 99.0.

The experiment therefore moved the uplink by 10 dB in amplitude (20 dB in
power), and left the gNB's receiver at 70× full scale — still deep into
whatever clipping there might be. It does not test the clipping hypothesis at
all. What it does test, and test well, is scale invariance:

| | `tx_gain = 50` | `tx_gain = 0` |
|---|---|---|
| ue0 TX peak amplitude | 313.1 | **99.0** |
| gnb0 RX peak amplitude | 212.4 | **70.0** (still 0.70 % over scale) |
| gnb0 PUSCH-OK SINR, median / max | 11.4 / 15.2 dB | **11.9 / 16.3 dB** |
| gnb1 PUSCH-OK SINR, median / max | 10.7 / 14.9 dB | **11.4 / 16.2 dB** |
| gnb0 PUSCH KO rate | 3 % | 5 % |
| gnb1 PUSCH KO rate | 59 % | **52 %** |
| Verdict | `ping_failed` | `passed` |

**A 20 dB change in uplink power left the SINR ceiling where it was** (~11 dB
median, ~16 dB max in every configuration). The ceiling is set by something
scale-invariant: neither this topology's noise floor (110 dB below the signal)
nor a distortion floor that tracks amplitude. The next section identifies it.

The verdict flip to `passed` should not be read as a fix either: gnb1's PUSCH
KO rate barely moved, 59 % → 52 %. Both runs leave ue1 in the same marginal
state, and whether its ping happens to land at the verdict instant is close to
a coin flip. **This is run-to-run variance, not a repaired uplink.**

Note also which cell is marginal: gnb0 (serving ue0, at 88 dB inter-cell SIR)
decodes 95–97 % of PUSCH, while gnb1 (serving ue1, at 4.6 dB) fails half of
it — at an identical SINR ceiling. That points the residual instability back
at ue1's downlink margin rather than at the uplink: a UE that misses downlink
grants does not transmit, and the scheduler logs those occasions as `crc=KO`.
The extreme KO outliers at gnb1 (−55 dB, −77 dB) are consistent with empty
occasions rather than failed decodes.

One correction stands regardless. Earlier notes in this workstream — and the
comment in `topology.sionna-multi-gnb.cuda.yaml` about the uplink riding on an
unverified assumption — pointed at an uncalibrated uplink `noise_power`. The
uplink noise floor is measurably irrelevant, 110 dB below the signal. Whatever
sets the +16 dB ceiling, it is not the noise floor and not the sample scale.

## The +16 dB ceiling is not a ceiling: it is the DM-RS estimate residual

There is no fixed ceiling. The reported PUSCH SINR is a **monotone function of
the allocation bandwidth**, and it is the same function in both cells, in every
run, at every power level. Grouping every `crc=OK` PUSCH line by
`prb=[a, b)` width:

| PUSCH PRBs | gnb0 median / max | gnb1 median / max |
|---|---|---|
| 1 | 15.0 / 16.3 dB | 15.1 / 16.2 dB |
| 5 | 13.0 / 14.5 dB | 12.7 / 13.8 dB |
| 24 | 11.0 / 13.5 dB | 11.1 / 12.1 dB |
| 36 | — | 9.2 / 9.3 dB |
| 106 | — | **7.3 / 7.4 dB** |

(run `20260914T144228Z`; rows with fewer than 3 samples omitted.)

PUCCH lands on the same curve. Restricted to occasions that actually carried a
detected SR, PUCCH — 1 or 2 PRB — reports median **13.7 dB**, max 15.3 dB at
gnb0 and 14.8 dB at gnb1, i.e. the 1-PRB PUSCH point. It uses the same
estimator with the same strategies (`lib/phy/upper/signal_processors/pucch/
factories.cpp:36`).

The "~16 dB max" quoted earlier was simply the narrowest allocations in the
mix, and the "~11 dB median" was the 24-PRB grants that dominate the sample.

Held at a fixed allocation width, the number is invariant to everything else:

| | `20260914T133439Z` | `20260914T142546Z` | `20260914T144228Z` | `20260914T121458Z` |
|---|---|---|---|---|
| gnb0, 24 PRB, median | 10.6 dB | 10.6 dB | 11.0 dB | 10.4 dB |
| gnb1, 24 PRB, median | — | 10.6 dB | 11.1 dB | 10.6 dB |

Those runs differ by 20 dB of uplink power, by whether the crosstalk edges
exist, by wire capture on or off, and by 84 dB of inter-cell SIR between the
two cells — and they agree within 0.7 dB.

**A noise-limited SINR would not depend on the allocation width at all**: the
reported figure is per-RE, and thermal noise per RE does not care how many RBs
the grant spans. A clipping distortion floor would not either, and would in any
case have moved with the 20 dB power change. A bandwidth-dependent,
power-invariant SINR is the signature of a **channel-estimate residual**.

### Where it comes from in the gNB

`port_channel_estimator_average_impl.cpp` estimates noise as the energy left
over after the *smoothed* channel estimate is subtracted from the received
pilots (`estimate_noise`, line 763; it subtracts `filtered_pilots_lse`, not the
raw LSE). Anything the smoother cannot follow is therefore booked as noise. The
smoother is a raised-cosine low-pass over frequency
(`port_channel_estimator_fd_smoothing_strategy::filter`, the default set at
`upper_phy_factories.cpp:568`), so the wider the grant, the more of the
channel's frequency variation falls outside what it tracks — and the more of
the signal is re-labelled as noise. Both the residual and the signal scale with
transmit power, which is exactly why the result is scale-invariant.

That residual reaches the log even though the gNB's default SINR method is not
the channel estimator: `pusch_sinr_calc_method` defaults to `post_equalization`
(`du_low_config.h:41`), and the post-equalization path is fed the estimator's
own figure — `pusch_demodulator_impl.cpp:337` passes
`est_results.get_noise_variance(i_port)` into the equalizer and line 436 turns
the resulting per-RE variances into the logged `sinr=`. Same root.

Two negative findings worth recording:

- The estimator's explicit clamp is **not** involved. `MAX_SINR_DB = 100`
  (`port_channel_estimator_average_impl.h:29`) bounds `noise_var` from below at
  100 dB, three decades above anything observed.
- **`pusch_channel_estimator_fd_strategy` is dead config.**
  `upper_phy_factories.cpp:568-574` selects the *frequency-domain* strategy by
  reading `config.pusch_channel_estimator_td_strategy`. The `fd_strategy`
  string is translated (`du_low_config_translator.cpp:49`) and then never read.
  Anyone testing this has to write the FD strategy into the **TD** key.

### The confirming experiment

Set, in `gen_gnb_config`'s output:

```yaml
expert_phy:
  pusch_channel_estimator_td_strategy: none   # sets the FD strategy, per the bug above
```

Prediction: the reported PUSCH SINR jumps by tens of dB and loses its
dependence on the allocation width, because with no smoothing the residual
against the raw LSE is zero and `noise_var` falls to the `MAX_SINR_DB` floor.
The independent cross-check is `pusch_sinr_calc_method: evm`, which measures
the demodulated symbols rather than the DM-RS: if EVM-SINR also reports ~11 dB
with the same bandwidth slope, the limit is real distortion after all and this
section is wrong.

Either way the practical consequence is already clear, and it is the one the
4T4R fixture reached independently: `pusch.max_ue_mcs: 9` exists because UL
link adaptation trusts this number. The comment in
`examples/ocudu/gnb_zmq_b210_fdd_4t4r_rank1_srsue.yaml` calls it "the gNB's own
SNR estimate, which saturates at ~11 dB on this noiseless digital channel" —
that is the same quantity, and the measurement above says the 11 dB is the
24-PRB point of a bandwidth curve rather than a saturation level.

## The 56.8 dB TX scale gap is fully accounted for

There is no unexplained 47 dB. The gap decomposes exactly into two deliberate,
opposite-signed gain stages:

| Term | Value | Source |
|---|---|---|
| srsUE `tx_gain` | **+50.00 dB** | `write_srsue_config`, applied as a sample scale at `rf_zmq_imp.c:974` |
| srsUE baseband peak | 0.990 | 313.1 / 10^(50/20) — essentially full scale |
| gNB amplitude-control back-off | **−43.05 dB** | `ru_sdr_config_translator.cpp:68` |
| gNB baseband peak before it | 63.9 | 0.45 / 10^(−43.05/20) |
| **Wire peak ratio** | **56.85 dB** | 20·log10(313.1 / 0.45) = 56.84 dB measured |

The gNB's stage is
`input_gain_dB = −convert_power_to_dB(bandwidth_sc) − gain_backoff_dB`, i.e.
−10·log10(106 × 12) − 12 = **−43.045 dB**, applied by
`amplitude_controller_clipping_impl` as an *amplitude* multiplier
(`convert_dB_to_amplitude`, 10^(x/20)) = 7.043e-3. It undoes the DFT's power
normalisation and reserves 12 dB of PAPR headroom, so a correctly scaled OFDM
signal lands just under full scale. It is not a bug and not tunable from the
gate's config path (`gain_backoff_dB = 12.0F`,
`ru_sdr_config.h:47`); `tx_gain: 0` in the gNB YAML is the *radio* gain, a
different stage entirely.

So the two radios are each doing the right thing for their own architecture and
the 56.8 dB is the sum of a +50 dB knob the gate chose and a −43 dB back-off
srsRAN applies by design, offset by the 36.2 dB difference in their raw
baseband peaks. Nothing in the link budget is unaccounted for, and no absolute
`noise_power` in these topologies is harder to reason about because of it —
though it does mean the two directions sit at wildly different sample scales,
so a single absolute `noise_power` cannot be right for both.

## Harness bug (fixed): the gate hung instead of tearing down

The Sionna side is `run_web_ui.sh`, a bash wrapper whose children are the RT
bridge and the Web UI server. Teardown did:

```bash
kill -INT "${sionna_pid}"; wait "${sionna_pid}"
```

A non-interactive shell does not forward signals to its children, so the
wrapper survived, both children kept running, and `wait` never returned. The
bridge then traced and logged at ~88 MB/min until something killed it:
**15 hours and 13 GB** on 2026-09-13, 48 minutes and 4.3 GB on 2026-09-14. It
also means `sionna_updates` and `rx_starvations` in any summary from a hung run
are whole-hang totals, not gate measurements — the baseline's
`sionna_updates: 81580` covers 15 hours, not 60 seconds.

Fixed with a `stop_sionna()` helper — children first, then the wrapper, with a
bounded SIGKILL fallback — wired into both the normal teardown and the `EXIT`
trap of `ocudu-multi-gnb-smoke.sh` and `ocudu-multi-ue-smoke.sh`, which shared
the bug. Verified: the next run exited on its own after its 90 s hold, left no
stray processes or containers, and wrote 460 MB instead of 4.3 GB.

## Changes made

| Change | File |
|---|---|
| `stop_sionna()` + 4 call sites | `scripts/remote/ocudu-multi-gnb-smoke.sh`, `scripts/remote/ocudu-multi-ue-smoke.sh` |
| `OCUDU_MGNB_BROKER_EXTRA_ARGS` passthrough (for `--wire-capture-*`) | `scripts/remote/ocudu-multi-gnb-smoke.sh` |
| `OCUDU_MGNB_UE_TX_GAIN` knob, default 50 | `scripts/remote/ocudu-multi-gnb-smoke.sh` |
| Crosstalk-free diagnostic topology | `examples/topology.sionna-multi-gnb-noxtalk.cuda.yaml` |
| Crosstalk-free diagnostic scenario | `examples/sionna/sutd/2gnb-2ue-overlap-noxtalk.json` |

## Still open

1. ~~Identify what sets the +16 dB uplink SINR ceiling.~~ **Answered** — it is
   not a ceiling but the gNB's DM-RS channel-estimate residual, a monotone
   function of allocation bandwidth (15.0 dB at 1 PRB to 7.3 dB at 106 PRB) and
   invariant to power, crosstalk and inter-cell SIR. See "The +16 dB ceiling is
   not a ceiling" above. **Remaining**: run the one confirming experiment
   (`pusch_channel_estimator_td_strategy: none`, plus `pusch_sinr_calc_method:
   evm` as the independent cross-check) to turn the attribution into a
   measurement.
2. ~~Explain the 56.8 dB TX scale gap between srsUE and the OCUDU gNB.~~
   **Answered** — +50 dB of srsUE `tx_gain` against −43.045 dB of srsRAN
   amplitude-control back-off, over a 36.2 dB difference in raw baseband peaks;
   the three close to the measured 56.84 dB. See "The 56.8 dB TX scale gap is
   fully accounted for" above. Nothing is unexplained. The residual worry is
   the opposite one: the two directions sit at very different sample scales, so
   one absolute `noise_power` cannot be calibrated for both.
2b. **`OCUDU_MGNB_UE_TX_GAIN=0` does not do what it says.** srsRAN_4G falls
   back to `rx_gain` (40) whenever `tx_gain <= 0` (`radio.cc:162`), so the knob
   silently has a floor of 40 dB and cannot reach the ~47 dB reduction a real
   clipping test needs. Either raise `rx_gain`'s coupling out of the way or
   drive the scale through the bridge's `gain_offset_db` instead.
3. **Land the crosstalk fix permanently**: `path_loss_db: 60` on the
   `crosstalk` model of the tracked topology, citing Table 6.5.3.2-1.
4. **ue1's inter-cell margin is the most likely residual cause** — 4.6 dB median with crosstalk
   gone, and the 60 s gate only samples the benign part of its wander. Over
   300 s the same geometry reaches −0.6 dB. Both cells share `dl_arfcn 368500`
   with no measurement configuration and no handover, and the two gNBs each run
   their own CU-CP so there is no Xn between them. A single gNB with two cells
   would be the structurally correct way to add intra-CU handover.
5. **Two documentation errors found while reading the noise path.**
   `topology.sionna-multi-gnb.cuda.yaml` lines 37–41 say `noise_power` is
   converted to an SNR against a reference power of 1.0 and therefore tracks
   receive power; lines 207–212 of the same file say it is absolute.
   `mutable_params.h` lines 26–31 agree with the former. The code agrees with
   the latter — `mutable_params.cpp:49` leaves `awgn_snr_db` at its default
   whenever a chain names an explicit `noise_power`. Reasoning from the stale
   comments leads directly to the wrong conclusion that uplink and downlink
   scale differences cancel themselves.
6. **The receiver model is not control-addressable**, which matters if anyone
   wants a geometry-tracking noise floor. `cuda_backend.cu:1096` builds the
   `rx_model` with a `nullptr` live-parameter pointer and `init_model_state`
   never populates `live`, so inside an `rx_model` a `path_loss` step scales by
   1, `cfo` is inert, and an `awgn` step using `snr_db` produces exactly zero
   noise because the running power is 0. Only an absolute `noise_power` works.
   Wiring live parameters into `rx_model` is a prerequisite for any TDD /
   cross-link-interference scenario, where UE-to-UE coupling is dominant and
   time-varying rather than negligible.
7. **Only port 0 of each cell radiates.** `gnb0_p1..p3` and `gnb1_p1..p3`
   captured all zeros, so the 4-antenna claim is not being exercised on the
   downlink in this scene.
8. **`results/logs/ocudu-multi-gnb` is 18 GB**, mostly from the two hung runs.
9. **`pusch_channel_estimator_fd_strategy` is inert in the gNB tree.**
   `upper_phy_factories.cpp:568-574` picks the frequency-domain smoothing
   strategy from `pusch_channel_estimator_td_strategy`; the `fd_strategy`
   string is plumbed all the way through
   (`du_low_config_translator.cpp:49`) and then never read. Setting it has no
   effect, and setting the TD key changes the FD behaviour instead. Belongs
   upstream in the OCUDU gNB tree
   (`~/ocudu-native-workspace/src/ocudu`), not here.
