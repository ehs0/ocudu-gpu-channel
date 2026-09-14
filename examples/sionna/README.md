# Sionna RT scenario configs

A scenario is the dependency-free JSON contract shared by the launcher, the
bridge and the config renderers. `run_bridge.py --scenario-config <file>`
reads it, and the native launchers pass it through
`OCUDU_NATIVE_SIONNA_SCENARIO`.

| File | Scene | Nodes | Links | Runs on the native gate |
| --- | --- | --- | --- | --- |
| **`ocudu-rank1-sutd.json`** | `sionna_SUTD_test` | 1 gNB (4T4R) + 1 car UE | 2 | ✅ **default for `run-ocudu-sionna-rank1.sh`** |
| `ocudu-rank1.json` | `sionna_simple_test` | 1 gNB (4T4R) + 1 car UE | 2 | ✅ |
| `ocudu-docker.json` | `sionna_simple_test` | 1 gNB + 1 UE, 1×1 | 2 | ✅ via `run-ocudu-sionna-1x1.sh` |
| `sionna_SUTD_test.json` | `sionna_SUTD_test` | 1 gNB + car UE + pedestrian UE | 5 | ❌ two UEs |
| `ocudu-docker-multi-ue.json` | `sionna_simple_test` | 1 gNB + 2 UEs | 4 | ❌ two UEs |
| `multi-gnb.json`, `graph.json` | `sionna_simple_test` | 2 gNB + 2 UE | 8 / 6 | ❌ two gNBs |

The native Sionna gates start one gNB and one srsUE process, so a scenario
with more of either traces fine through the bridge and the web UI but cannot
drive the live radio stacks. `render-sionna-rank1-configs.py` says so by name
when it refuses one.

## The 1 gNB / 1 UE example

`ocudu-rank1-sutd.json` is the reference single-cell setup:

- **gNB** — 4T4R, fixed, on the parapet of SUTD Building 2 at 30.5 m
  (roof 24.5 m plus a 6 m mast). At the roof *centre* the building's own roof
  cuts every path to the street, which is why the mast sits at the edge.
- **UE** — a car shuttling a 76 m stretch of the campus service road that runs
  along Building 2's north face, at 8.33 m/s (30 km/h), round trip 18.3 s.
- **Links** — one downlink and one uplink, both `sionna_rt`.
- **Scene** — `sionna_SUTD_test`, built from OpenStreetMap
  (see `scenes/sionna_SUTD_test/LICENSE.md`: the data is ODbL 1.0).

## Fields

| Key | Meaning |
| --- | --- |
| `scene` | scene XML path, a directory under `scenes/`, or a built-in Sionna scene |
| `simple_road` | `false` for a scene that ships its own ground and roads |
| `nodes.<id>.tx_array` / `rx_array` | `rows`×`cols`; the native renderer takes these as the source of truth for gNB and broker port counts |
| `nodes.<id>.start_m` | position in scene metres; optional when `route_m` is given |
| `nodes.<id>.route_m` + `route_mode` + `speed_mps` | a walked polyline, `pingpong` or `loop` |
| `nodes.<id>.velocity_mps` + `route_x_m` | the older constant-velocity form, still supported |
| `links[]` | `from`, `to`, `direction` (`downlink`/`uplink`/`crosstalk`), `model` |
| `solver` | `max_depth`, `samples_per_source`, `seed`, `path_polylines`, and a `propagation` block of the five mechanisms |

Anything the `solver` block omits keeps the command-line default, so a
scenario pins only what it means to pin. Which mechanisms are on decides how
many paths exist at all: with the default `los` + `specular_reflection` and
nothing else, the SUTD drop-off link carries 2 paths and the UE-to-UE link 11.
