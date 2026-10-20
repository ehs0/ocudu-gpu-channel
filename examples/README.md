# examples

Application demos, one folder each (code, gate script and README together),
plus `configs/`, the input configurations the demos and gates read.

| Demo | Gate (run from the repo root) |
|---|---|
| [robot_fight/](robot_fight/README.md) — sumo-robot arena over two cells, two brokers sharing one GPU | `bash examples/robot_fight/run-ocudu-robot-fight.sh` |
| [ran_benchmark/](ran_benchmark/README.md) — OCUDU MAC schedulers and link adaptation (fixed MCS, AMC, AMC + OLLA) under one seeded Sionna channel | `bash examples/ran_benchmark/run-ocudu-scheduler-benchmark.sh` |

**RAN benchmarks.** `ran_benchmark/` is the first RAN benchmark: it runs
live OCUDU gNB and srsUE stacks so that the alternatives under test see the same
channel and traffic. Benchmarks for other RAN functions will be added here as
their own folders.

The demo gates reuse the shared native tooling in `scripts/native/` (`env.sh`,
`oai-gate-defaults.sh`, workspace lock, base renderers). Run outputs land
under `$OCUDU_NATIVE_ROOT/{results,configs,data,run}/`, not here.

[`configs/`](configs/README.md) holds broker topologies, Sionna scenarios and
scenes, and the gNB / UE / core configs.
