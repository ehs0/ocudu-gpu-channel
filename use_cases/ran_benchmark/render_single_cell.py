#!/usr/bin/env python3
"""Render one single-cell policy run: one gNB, one broker topology, N UEs.

Exactly one scheduler policy and one link adaptation (fixed MCS, CQI/SINR
AMC, AMC + OLLA; definitions.LINK_ADAPTATION) per gNB config. Everything else
-- gNB identity and addresses, the UE identities (definitions.single_cell_ue),
the receiver noise floor, the subscriber 5QIs -- depends only on the UE count
and the traffic profile, so the configs of two policy runs of one session
differ in the policy block and the pdsch / pusch link-adaptation keys alone.

Reused unchanged: the gNB rendering of render-multi-gnb-configs.py (cell a's
identity), the Open5GS and srsUE rendering of render-multi-ue-configs.py, the
receiver-noise arithmetic of render-sionna-multi-ue-configs.py and the gNB
metrics block of render-sionna-rank1-configs.py.

The gNB antenna count is read off the scenario's gnb0 array (the Sionna
node arrays are the source of truth, as in render-sionna-rank1-configs.py):
1 renders the SISO cell, 2 the rank-1 2T2R cell of the R2 fixture
(use_cases/configs/ran/ocudu/docker/gnb_zmq_b210_fdd_2t2r_rank1_srsue.yaml)
with its three measured adaptations, applied by `render_gnb_antennas`.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NATIVE = HERE.parents[1] / "scripts" / "native"
sys.path.insert(0, str(HERE))

import definitions as bench  # noqa: E402


def load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


multi_gnb = load("render_multi_gnb_configs", NATIVE / "render-multi-gnb-configs.py")
sionna = load("render_sionna_multi_ue_configs", NATIVE / "render-sionna-multi-ue-configs.py")
rank1 = load("render_sionna_rank1_configs", NATIVE / "render-sionna-rank1-configs.py")
legacy = multi_gnb.multi_ue
fail = legacy.fail

GNB_CELL = {**bench.SINGLE_CELL_GNB, "pci": 1, "gnb_id": 411, "bind": "127.0.0.11"}
SUBSCRIBER_HEADER = "# name,imsi,key,op_type,op_or_opc,amf,qci,ipv4\n"
SUBSCRIBER_KEY = "00112233445566778899aabbccddeeff"
SUBSCRIBER_OPC = "63bfa50ee6523365ff14c1f45f88737d"


def insert_scheduler_policy(gnb_text: str, scheduler: str) -> str:
    if scheduler not in bench.SCHEDULERS:
        fail(f"unknown scheduler {scheduler!r}; known: {', '.join(bench.SCHEDULERS)}")
    if "\n  scheduler:\n" in gnb_text:
        fail("gNB fixture already has a cell_cfg.scheduler block")
    return legacy.replace_exact(gnb_text, "cell_cfg:\n", "cell_cfg:\n" + bench.SCHEDULERS[scheduler]["yaml"], 1,
                                "gNB scheduler policy")


def insert_link_adaptation(gnb_text: str, test: dict) -> str:
    """Append the test's link-adaptation keys (definitions.link_adaptation_yaml)
    to cell_cfg.pdsch and cell_cfg.pusch."""

    try:
        lines = bench.link_adaptation_yaml(test)
    except ValueError as error:
        fail(str(error))
    for section, text in lines.items():
        for key in text.split():
            if key.endswith(":") and f"    {key}" in gnb_text:
                fail(f"gNB fixture already sets {key[:-1]}")
    for section, text in lines.items():
        anchor = f"  {section}:\n    mcs_table: qam64\n"
        gnb_text = legacy.replace_exact(gnb_text, anchor, anchor + text, 1, f"gNB {section} link adaptation")
    return gnb_text


def link_adaptation_test(args: argparse.Namespace) -> dict:
    """The run's link-adaptation fields ({"link_adaptation", ["fixed_dl_mcs", "fixed_ul_mcs"]}), checked."""

    test = {"scheduler": args.policy, "link_adaptation": args.link_adaptation}
    for key in ("fixed_dl_mcs", "fixed_ul_mcs"):
        if getattr(args, key) is not None:
            test[key] = getattr(args, key)
    try:
        test = bench.normalize_test(test)
    except ValueError as error:
        fail(str(error))
    del test["scheduler"]
    return test


def add_config_dump(gnb_text: str) -> str:
    return legacy.replace_exact(gnb_text, "  all_level: info\n", "  all_level: info\n  config_level: info\n", 1,
                                "gNB config dump level")


def array_count(node: dict, key: str) -> int:
    array = node.get(key, node.get("array")) or {}
    return int(array.get("rows", 1)) * int(array.get("cols", 1))


def check_scenario(root: dict, ue_count: int) -> int:
    """Check the scenario's shape and return the gNB antenna count."""

    nodes = root.get("nodes")
    expected = {"gnb0"} | {f"ue{i}" for i in range(ue_count)}
    if not isinstance(nodes, dict) or set(nodes) != expected:
        fail(f"single-cell scenario must name exactly gnb0 and ue0..ue{ue_count - 1}")
    antennas = array_count(nodes["gnb0"], "tx_array")
    if array_count(nodes["gnb0"], "rx_array") != antennas or antennas not in bench.GNB_ANTENNA_CONFIGS:
        fail(f"gnb0 must have equal TX/RX arrays of {' or '.join(map(str, bench.GNB_ANTENNA_CONFIGS))} antennas")
    for index in range(ue_count):
        if (array_count(nodes[f"ue{index}"], "tx_array"), array_count(nodes[f"ue{index}"], "rx_array")) != (1, 1):
            fail(f"ue{index} must be a one-antenna srsUE")
    pairs = {(link.get("from"), link.get("to")) for link in root.get("links") or []}
    for index in range(ue_count):
        if ("gnb0", f"ue{index}") not in pairs or (f"ue{index}", "gnb0") not in pairs:
            fail(f"scenario is missing the gnb0<->ue{index} link pair")
    if len(pairs) != 2 * ue_count:
        fail("single-cell scenario carries gNB<->UE links only")
    return antennas


# The rank-1 2T2R cell (R2 of RANK1_MILESTONES.md, see the fixture named in
# the module docstring), each change measured there:
#   - SS#2 ue_dedicated with DCI 0_1/1_1: OCUDU rejects fallback DCI with
#     nof_antennas_dl > 1 (du_cell_config_validation.cpp:290);
#   - CSI-RS off: OCUDU sizes the NZP-CSI-RS by nof_antennas_dl
#     (lib/scheduler/config/builder/csi_meas_config.cpp:22) and srsUE cannot
#     apply a 2-port resource, so connection setup fails. Without CSI reports
#     the DL CQI stays at the scheduler's initial_cqi (3) plus OLLA
#     (scheduler_expert_config.h:144,183), for every policy alike;
#   - MAC pcap off: rejected with more than one DL antenna.
GNB_2T2R_CHANGES = (
    ("      ss2_type: common\n", "      ss2_type: ue_dedicated\n", "SS#2 type"),
    ("      dci_format_0_1_and_1_1: false\n", "      dci_format_0_1_and_1_1: true\n", "non-fallback DCI"),
    ("  pdsch:\n", "  csi:\n    csi_rs_enabled: false\n  pucch:\n    nof_cell_csi_res: 0\n  pdsch:\n", "CSI-RS off"),
    ("  mac_enable: enable\n", "  mac_enable: disable\n", "MAC pcap off"),
)


def render_gnb_antennas(gnb_text: str, antennas: int) -> str:
    """The ZMQ ports of every antenna, and the 2T2R cell for antennas > 1."""

    ports = bench.single_cell_gnb_ports(antennas)
    single = f"tx_port=tcp://127.0.0.1:{ports[0]['tx_port']},rx_port=tcp://127.0.0.1:{ports[0]['rx_port']}"
    if antennas == 1:
        if gnb_text.count(single) != 1:
            fail("gNB ZMQ endpoints missing or ambiguous")
        return gnb_text
    endpoints = ",".join([f"tx_port{i}=tcp://127.0.0.1:{p['tx_port']}" for i, p in enumerate(ports)]
                         + [f"rx_port{i}=tcp://127.0.0.1:{p['rx_port']}" for i, p in enumerate(ports)])
    gnb_text = legacy.replace_exact(gnb_text, single, endpoints, 1, "gNB ZMQ port endpoints")
    gnb_text = legacy.replace_exact(gnb_text, "cell_cfg:\n",
                                    f"cell_cfg:\n  nof_antennas_dl: {antennas}\n  nof_antennas_ul: {antennas}\n", 1,
                                    "gNB antenna count")
    for old, new, label in GNB_2T2R_CHANGES:
        gnb_text = legacy.replace_exact(gnb_text, old, new, 1, label)
    return gnb_text


def render_topology(ue_count: int, rx_noise: dict[str, float], gnb_antennas: int = 1) -> str:
    """The broker topology: gnb0 (one port per antenna) and ue0..ueN-1, one
    quiet TDL per link model, one absolute AWGN receiver floor per node
    (render-sionna-multi-ue-configs.py semantics, with endpoints from
    definitions.single_cell_ue). Every gNB port carries the node's rx_model;
    the broker keeps one noise state per receive row, so the floors of the
    two ports are independent draws."""

    nodes = [("gnb0", [(p["tx_port"], p["rx_port"]) for p in bench.single_cell_gnb_ports(gnb_antennas)])]
    nodes += [(ue["device_id"], [(ue["tx_port"], ue["rx_port"])]) for ue in map(bench.single_cell_ue, range(ue_count))]
    devices = ""
    for node_id, ports in nodes:
        for index, (tx_port, rx_port) in enumerate(ports):
            devices += (f"  - id: {node_id}_p{index}\n    role: port\n    sample_rate_hz: 23040000\n"
                        f"    tx_endpoint: tcp://127.0.0.1:{tx_port}\n    rx_endpoint: tcp://127.0.0.1:{rx_port}\n")
            if node_id in rx_noise:
                devices += f"    rx_model: rx_noise_{node_id}\n"
    radio_nodes = ""
    for node_id, ports in nodes:
        port_lines = "".join(f"      - {node_id}_p{index}\n" for index in range(len(ports)))
        radio_nodes += f"  - id: {node_id}\n    tx_ports:\n{port_lines}    rx_ports:\n{port_lines}"
    links = "".join(f"  - from: gnb0\n    to: ue{i}\n    model: sionna_rt\n" for i in range(ue_count))
    links += "".join(f"  - from: ue{i}\n    to: gnb0\n    model: sionna_rt\n" for i in range(ue_count))
    models = ("  sionna_rt:\n    chain:\n      - type: tdl\n        taps:\n          - delay_samples: 0.0\n"
              "            gain_db: -100.0\n            phase_rad: 0.0\n")
    models += "".join(f"  rx_noise_{node_id}:\n    chain:\n      - type: awgn\n        noise_power: {rx_noise[node_id]:.6e}\n"
                      for node_id, _ in nodes if node_id in rx_noise)
    return ("# Generated by use_cases/ran_benchmark/render_single_cell.py.\n"
            "runtime:\n  backend: cuda\n  gpu_device: 0\n  batch_samples: 23040\n  queue_samples: 2457600\n"
            f"devices:\n{devices}radio_nodes:\n{radio_nodes}links:\n{links}models:\n{models}")


def render_subscriber(ue_count: int, five_qi: list[int]) -> str:
    rows = []
    for index in range(ue_count):
        ue = bench.single_cell_ue(index)
        rows.append(f"single-cell-{index},{ue['imsi']},{SUBSCRIBER_KEY},opc,{SUBSCRIBER_OPC},8000,"
                    f"{five_qi[index]},{ue['ipv4']}\n")
    return SUBSCRIBER_HEADER + "".join(rows)


def noise_description(rx_noise: dict[str, float], awgn_snr_db: float | None, tx_dl: float, tx_ul: float) -> dict:
    return {
        "enabled": bool(rx_noise),
        "awgn_snr_db": awgn_snr_db, "tx_power_dl": tx_dl, "tx_power_ul": tx_ul,
        "noise_power": dict(sorted(rx_noise.items())),
        "generator": ("CUDA Philox4_32_10 per receiver row: seed = std::hash(<node>:awgn:<step>), counter = samples "
                      "since the noise epoch; the replay bridge sets the epoch to measurement sample S0 on every node"),
        "applied": "once per receiver to the summed input (rx_model), never per propagation coefficient",
    }


def render(args: argparse.Namespace) -> dict:
    output_dir = legacy.safe_directory(args.output_dir, "output directory")
    log_dir = legacy.safe_directory(args.log_dir, "log directory")
    repo_root = args.repo_root.resolve(strict=True)
    native_root = args.native_root.resolve(strict=True)
    root = json.loads(args.scenario_config.read_text(encoding="utf-8"))
    antennas = check_scenario(root, args.ue_count)
    profile = bench.single_cell_profile(args.traffic_profile, args.ue_count)
    shape = sionna.LiveShape(sionna.NodePorts("gnb0", antennas, antennas),
                             tuple(sionna.NodePorts(f"ue{i}", 1, 1) for i in range(args.ue_count)),
                             tuple(sionna.ShapeLink(l["from"], l["to"], l.get("model", "sionna_rt"))
                                   for l in root["links"]))
    rx_noise = sionna.rx_noise_powers(shape, args.awgn_snr_db, args.tx_power_dl, args.tx_power_ul)
    gnb_source = legacy.read_regular(repo_root / "use_cases/configs/ran/ocudu/docker/gnb_zmq_b210_fdd_srsue.yaml",
                                     "gNB fixture")
    gnb_text = multi_gnb.render_gnb(gnb_source, GNB_CELL, log_dir)
    gnb_text = render_gnb_antennas(gnb_text, antennas)
    gnb_text = insert_scheduler_policy(gnb_text, args.policy)
    link_adaptation = link_adaptation_test(args)
    gnb_text = insert_link_adaptation(gnb_text, link_adaptation)
    gnb_text = add_config_dump(gnb_text)
    gnb_text += rank1.render_gnb_metrics(bench.SINGLE_CELL_GNB["metrics_port"])
    srsue_source = legacy.read_regular(repo_root / "use_cases/configs/ran/srsue/srsue_zmq_multi_ue.conf.in",
                                       "native srsUE template")
    open5gs_source = legacy.read_regular(native_root / "src/ocudu/docker/open5gs/open5gs-5gc.yml",
                                         "pinned OCUDU Open5GS template")
    rendered = {
        "gnb0.yaml": gnb_text,
        "topology.yaml": render_topology(args.ue_count, rx_noise, antennas),
        "open5gs.yaml": legacy.render_open5gs(open5gs_source, native_root),
        "subscriber.csv": render_subscriber(args.ue_count, [ue["five_qi"] for ue in profile["ues"]]),
        "scenario.json": json.dumps(root, indent=2) + "\n",
    }
    ues = []
    for index in range(args.ue_count):
        record = bench.single_cell_ue(index)
        rendered[f"srsue-ue{index}.conf"] = legacy.render_srsue(srsue_source, record, log_dir)
        ue_profile = profile["ues"][index]
        ues.append({**record, "index": index, "role": ue_profile["role"], "five_qi": ue_profile["five_qi"],
                    "qos": bench.FIVE_QI[ue_profile["five_qi"]], "flows": ue_profile["flows"]})
    metadata = {
        "kind": "ocudu-single-cell-scheduler-benchmark",
        "policy": args.policy,
        "scheduler_info": bench.SCHEDULERS[args.policy],
        "link_adaptation": link_adaptation,
        "link_adaptation_info": {**bench.LINK_ADAPTATION[link_adaptation["link_adaptation"]],
                                 "parameters": bench.link_adaptation_parameters(link_adaptation)},
        "ue_count": args.ue_count,
        "seed": args.seed,
        "traffic_profile": profile,
        "gnb": dict(GNB_CELL, metrics_enabled=True, antennas=antennas,
                    antenna_config=bench.GNB_ANTENNA_CONFIGS[antennas]["label"],
                    ports=bench.single_cell_gnb_ports(antennas)),
        "ues": ues,
        "links": [{"from": l["from"], "to": l["to"], "model": l.get("model", "sionna_rt")} for l in root["links"]],
        "noise": noise_description(rx_noise, args.awgn_snr_db, args.tx_power_dl, args.tx_power_ul),
        "scenario_benchmark": root.get("benchmark"),
    }
    for name, text in rendered.items():
        if name.endswith((".yaml", ".conf", ".csv")) and legacy.PLACEHOLDER_RE.search(text):
            fail(f"unresolved placeholder in {name}")
        legacy.write_new(output_dir / name, text)
    legacy.write_new(output_dir / "benchmark.json", json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--native-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--scenario-config", type=Path, required=True)
    parser.add_argument("--policy", choices=sorted(bench.SCHEDULERS), required=True)
    parser.add_argument("--link-adaptation", choices=sorted(bench.LINK_ADAPTATION),
                        default=bench.TEST_FIELD_DEFAULTS["link_adaptation"])
    parser.add_argument("--fixed-dl-mcs", type=int, default=None, help="with --link-adaptation fixed")
    parser.add_argument("--fixed-ul-mcs", type=int, default=None, help="with --link-adaptation fixed")
    parser.add_argument("--ue-count", type=int, required=True)
    parser.add_argument("--traffic-profile", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--awgn-snr-db", type=float, default=None)
    parser.add_argument("--tx-power-dl", type=float, default=sionna.TX_POWER_DL)
    parser.add_argument("--tx-power-ul", type=float, default=sionna.TX_POWER_UL)
    parser.add_argument("--noise-only", action="store_true",
                        help="print the receiver-noise description as JSON and exit (no files)")
    args = parser.parse_args()
    if not 1 <= args.ue_count <= bench.MAX_SINGLE_CELL_UES:
        parser.error(f"--ue-count must be 1..{bench.MAX_SINGLE_CELL_UES}")
    if args.noise_only:
        shape = sionna.LiveShape(sionna.NodePorts("gnb0", 1, 1),
                                 tuple(sionna.NodePorts(f"ue{i}", 1, 1) for i in range(args.ue_count)), ())
        rx_noise = sionna.rx_noise_powers(shape, args.awgn_snr_db, args.tx_power_dl, args.tx_power_ul)
        print(json.dumps(noise_description(rx_noise, args.awgn_snr_db, args.tx_power_dl, args.tx_power_ul),
                         sort_keys=True))
        return 0
    metadata = render(args)
    print(f"event=single_cell_configs_rendered policy={args.policy} "
          f"link_adaptation=\"{bench.link_adaptation_label(metadata['link_adaptation'])}\" ues={args.ue_count} "
          f"profile={metadata['traffic_profile']['name']} seed={args.seed} output_dir=\"{args.output_dir}\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
