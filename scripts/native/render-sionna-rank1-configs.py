#!/usr/bin/env python3
"""Render a native rank-1 live stack from one Sionna scenario.

The Sionna node arrays are the source of truth for the OCUDU antenna counts,
ZMQ endpoint count, Broker radio-node dimensions, and dynamic matrix link ids.
The live srsUE remains a one-port radio, so only the gNB arrays may scale.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

SUPPORTED_GNB_PORTS = frozenset((1, 2, 4))
MODEL_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


def load_legacy_renderer():
    path = Path(__file__).resolve().with_name("render-legacy-1x1-configs.py")
    spec = importlib.util.spec_from_file_location("render_legacy_1x1_configs", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load legacy renderer module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legacy = load_legacy_renderer()


@dataclass(frozen=True)
class LiveShape:
    gnb_tx: int
    gnb_rx: int
    ue_tx: int
    ue_rx: int
    downlink_model: str
    uplink_model: str


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    return value


def _array_count(node: dict[str, Any], key: str, where: str) -> int:
    raw = node.get(key, node.get("array"))
    if raw is None:
        raw = {}
    array = _object(raw, f"{where}.{key}")
    rows = array.get("rows", 1)
    cols = array.get("cols", 1)
    if (
        not isinstance(rows, int)
        or isinstance(rows, bool)
        or rows <= 0
        or not isinstance(cols, int)
        or isinstance(cols, bool)
        or cols <= 0
    ):
        raise ValueError(f"{where}.{key} rows and cols must be positive integers")
    return rows * cols


def load_live_shape(path: Path) -> LiveShape:
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load Sionna scenario {path}: {error}") from error
    root = _object(root, "scenario")
    nodes = _object(root.get("nodes"), "scenario.nodes")
    if set(nodes) != {"gnb0", "ue0"}:
        raise ValueError("native rank-1 scenario must contain exactly gnb0 and ue0")
    gnb = _object(nodes["gnb0"], "nodes.gnb0")
    ue = _object(nodes["ue0"], "nodes.ue0")
    gnb_tx = _array_count(gnb, "tx_array", "nodes.gnb0")
    gnb_rx = _array_count(gnb, "rx_array", "nodes.gnb0")
    ue_tx = _array_count(ue, "tx_array", "nodes.ue0")
    ue_rx = _array_count(ue, "rx_array", "nodes.ue0")
    if gnb_tx not in SUPPORTED_GNB_PORTS or gnb_rx not in SUPPORTED_GNB_PORTS:
        supported = ", ".join(str(value) for value in sorted(SUPPORTED_GNB_PORTS))
        raise ValueError(
            f"live OCUDU gNB arrays must resolve to {supported} ports; "
            f"scenario requested tx={gnb_tx}, rx={gnb_rx}"
        )
    if (ue_tx, ue_rx) != (1, 1):
        raise ValueError(
            "native srsUE is single-port; nodes.ue0 tx_array and rx_array must both resolve to 1"
        )

    raw_links = root.get("links")
    if not isinstance(raw_links, list) or len(raw_links) != 2:
        raise ValueError("native rank-1 scenario must contain exactly one DL and one UL link")
    models: dict[tuple[str, str, str], str] = {}
    for index, raw_link in enumerate(raw_links):
        link = _object(raw_link, f"links[{index}]")
        source = link.get("from")
        destination = link.get("to")
        direction = link.get("direction")
        model = link.get("model", "sionna_rt")
        if not isinstance(model, str) or MODEL_ID_RE.fullmatch(model) is None:
            raise ValueError(f"links[{index}].model must match {MODEL_ID_RE.pattern}")
        key = (source, destination, direction)
        if key in models:
            raise ValueError(f"duplicate native rank-1 link: {key}")
        models[key] = model
    try:
        downlink_model = models[("gnb0", "ue0", "downlink")]
        uplink_model = models[("ue0", "gnb0", "uplink")]
    except KeyError as error:
        raise ValueError("scenario links must be gnb0->ue0 downlink and ue0->gnb0 uplink") from error
    if len(models) != 2:
        raise ValueError("scenario contains an unsupported link for the single-gNB/single-UE runtime")
    return LiveShape(gnb_tx, gnb_rx, ue_tx, ue_rx, downlink_model, uplink_model)


def render_gnb(source: str, log_dir: Path, shape: LiveShape) -> str:
    required = (
        "  nof_antennas_dl: 4\n",
        "  nof_antennas_ul: 4\n",
        "      ss2_type: ue_dedicated\n",
        "      dci_format_0_1_and_1_1: true\n",
        "    csi_rs_enabled: false\n",
        "    nof_cell_csi_res: 0\n",
        "  mac_enable: disable\n",
        "    max_ue_mcs: 9\n",
    )
    for token in required:
        if source.count(token) != 1:
            legacy.fail(f"rank-1 gNB fixture invariant missing or ambiguous: {token!r}")
    device_line = next(
        (line for line in source.splitlines(keepends=True) if line.startswith("  device_args: ")),
        None,
    )
    if device_line is None or source.count(device_line) != 1:
        legacy.fail("rank-1 gNB device_args line is missing or ambiguous")
    tx_args = [f"tx_port{i}=tcp://127.0.0.1:{2000 + 2 * i}" for i in range(shape.gnb_tx)]
    rx_args = [f"rx_port{i}=tcp://127.0.0.1:{2001 + 2 * i}" for i in range(shape.gnb_rx)]
    rendered = source.replace(
        device_line,
        "  device_args: " + ",".join((*tx_args, *rx_args, "base_srate=23.04e6")) + "\n",
        1,
    )
    # The fixture is the 4T4R template, so a 4-port scenario asks for a
    # replacement that changes nothing. legacy.replace_exact rejects that:
    # its "token survived replacement" guard cannot tell a deliberate
    # no-op from a substitution that silently failed. That made 4 ports
    # unreachable even though SUPPORTED_GNB_PORTS advertises them, so the
    # no-op is skipped here instead of weakening the shared guard. The
    # `required` check above already proved each token appears once.
    for key, count, label in (
        ("nof_antennas_dl", shape.gnb_tx, "gNB DL antenna count"),
        ("nof_antennas_ul", shape.gnb_rx, "gNB UL antenna count"),
    ):
        if count != 4:
            rendered = legacy.replace_exact(
                rendered, f"  {key}: 4\n", f"  {key}: {count}\n", 1, label,
            )
    for placeholder, filename, label in (
        ("@GNB_LOG@", "gnb-internal.log", "gNB log path"),
        ("@GNB_MAC_PCAP@", "gnb_mac.pcap", "gNB MAC pcap path"),
        ("@GNB_NGAP_PCAP@", "gnb_ngap.pcap", "gNB NGAP pcap path"),
    ):
        rendered = legacy.replace_exact(rendered, placeholder, str(log_dir / filename), 1, label)
    if legacy.PLACEHOLDER_RE.search(rendered):
        legacy.fail("unresolved gNB placeholder")
    return rendered


def _port_lines(count: int) -> str:
    return "".join(f"      - gnb0_p{index}\n" for index in range(count))


def render_topology(shape: LiveShape) -> str:
    device_count = max(shape.gnb_tx, shape.gnb_rx)
    devices = "".join(
        f"  - id: gnb0_p{index}\n"
        "    role: port\n"
        "    sample_rate_hz: 23040000\n"
        f"    tx_endpoint: tcp://127.0.0.1:{2000 + 2 * index}\n"
        f"    rx_endpoint: tcp://127.0.0.1:{2001 + 2 * index}\n"
        for index in range(device_count)
    )
    model_ids = tuple(dict.fromkeys((shape.downlink_model, shape.uplink_model)))
    models = "".join(
        f"  {model_id}:\n"
        "    chain:\n"
        "      - type: tdl\n"
        "        taps:\n"
        "          - delay_samples: 0.0\n"
        "            gain_db: -100.0\n"
        "            phase_rad: 0.0\n"
        for model_id in model_ids
    )
    return (
        "# Generated from the Sionna scenario by render-sionna-rank1-configs.py.\n"
        "# Port ordering is the Broker matrix ordering; dynamic matrix_profile_swap\n"
        "# replaces the quiet startup TDL atomically before either radio starts.\n"
        "runtime:\n"
        "  backend: cuda\n"
        "  gpu_device: 0\n"
        "  batch_samples: 23040\n"
        "  queue_samples: 2457600\n"
        "devices:\n"
        f"{devices}"
        "  - id: ue0_p0\n"
        "    role: port\n"
        "    sample_rate_hz: 23040000\n"
        "    tx_endpoint: tcp://127.0.0.1:2101\n"
        "    rx_endpoint: tcp://127.0.0.1:2100\n"
        "radio_nodes:\n"
        "  - id: gnb0\n"
        "    tx_ports:\n"
        f"{_port_lines(shape.gnb_tx)}"
        "    rx_ports:\n"
        f"{_port_lines(shape.gnb_rx)}"
        "  - id: ue0\n"
        "    tx_ports:\n"
        "      - ue0_p0\n"
        "    rx_ports:\n"
        "      - ue0_p0\n"
        "links:\n"
        "  - from: gnb0\n"
        "    to: ue0\n"
        f"    model: {shape.downlink_model}\n"
        "  - from: ue0\n"
        "    to: gnb0\n"
        f"    model: {shape.uplink_model}\n"
        "models:\n"
        f"{models}"
    )


def self_test() -> None:
    shape = LiveShape(2, 4, 1, 1, "dl_dynamic", "ul_dynamic")
    topology = render_topology(shape)
    assert topology.count("      - gnb0_p0\n") == 2
    assert "      - gnb0_p3\n" in topology
    assert "fixed_mimo" not in topology
    assert "model: dl_dynamic" in topology and "model: ul_dynamic" in topology
    # Every advertised port count must actually render. 4 is the fixture's
    # own value, so it exercises the no-op replacement path.
    gnb_source = (
        Path(__file__).resolve().parents[2]
        / "examples/native/ocudu/gnb_zmq_b210_fdd_4t4r_rank1_srsue.yaml"
    ).read_text(encoding="utf-8")
    for ports in sorted(SUPPORTED_GNB_PORTS):
        rendered = render_gnb(
            gnb_source, Path("/tmp"), LiveShape(ports, ports, 1, 1, "dl", "ul")
        )
        assert f"  nof_antennas_dl: {ports}\n" in rendered
        assert f"  nof_antennas_ul: {ports}\n" in rendered
        assert f"tx_port{ports - 1}=" in rendered
        assert f"tx_port{ports}=" not in rendered
    assert _array_count({"tx_array": {"rows": 2, "cols": 2}}, "tx_array", "node") == 4
    try:
        _array_count({"rx_array": {"rows": 0, "cols": 1}}, "rx_array", "node")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid Sionna array dimensions were accepted")
    print("event=native_sionna_rank1_config_renderer_self_test result=pass")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--native-root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--scenario-config", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    render_args = (args.repo_root, args.native_root, args.output_dir, args.log_dir, args.scenario_config)
    if args.self_test:
        if any(render_args):
            parser.error("--self-test cannot be combined with render arguments")
        self_test()
        return 0
    if not all(render_args):
        parser.error("render mode requires repo/native/output/log/scenario arguments")

    repo_root = args.repo_root.resolve(strict=True)
    native_root = args.native_root.resolve(strict=True)
    scenario = args.scenario_config.resolve(strict=True)
    output_dir = legacy.safe_output_directory(args.output_dir)
    log_dir = legacy.safe_log_directory(args.log_dir)
    if repo_root != args.repo_root or native_root != args.native_root or scenario != args.scenario_config:
        legacy.fail("repo, native, and scenario paths must already be canonical")
    shape = load_live_shape(scenario)
    gnb_source = legacy.read_regular(
        repo_root / "examples/native/ocudu/gnb_zmq_b210_fdd_4t4r_rank1_srsue.yaml",
        "rank-1 4T4R gNB fixture",
    )
    open5gs_source = legacy.read_regular(
        native_root / "src/ocudu/docker/open5gs/open5gs-5gc.yml",
        "pinned OCUDU Open5GS template",
    )
    srsue_source = legacy.read_regular(
        repo_root / "examples/native/srsran/srsue_zmq_legacy_1x1.conf.in",
        "native srsUE template",
    )
    subscriber_source = legacy.read_regular(
        repo_root / "examples/native/open5gs/subscriber-legacy-1x1.csv",
        "native subscriber template",
    )
    rendered = {
        "gnb.yaml": render_gnb(gnb_source, log_dir, shape),
        "topology.yaml": render_topology(shape),
        "open5gs.yaml": legacy.render_open5gs(open5gs_source, native_root),
        "srsue.conf": legacy.render_srsue(srsue_source, log_dir),
        "subscriber.csv": legacy.validate_subscriber(subscriber_source),
    }
    for name, text in rendered.items():
        if legacy.PLACEHOLDER_RE.search(text):
            legacy.fail(f"unresolved placeholder in {name}")
        legacy.write_new(output_dir / name, text)
    metadata = {
        "scenario": str(scenario),
        "gnb_tx_ports": shape.gnb_tx,
        "gnb_rx_ports": shape.gnb_rx,
        "ue_tx_ports": shape.ue_tx,
        "ue_rx_ports": shape.ue_rx,
        "downlink_model": shape.downlink_model,
        "uplink_model": shape.uplink_model,
    }
    legacy.write_new(output_dir / "sionna-rank1-shape.json", json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    print(
        "event=native_sionna_rank1_configs_rendered "
        f"gnb_tx={shape.gnb_tx} gnb_rx={shape.gnb_rx} ue_tx={shape.ue_tx} ue_rx={shape.ue_rx} "
        f'output_dir="{output_dir}"'
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError) as error:
        print(f"config rendering failed: {error}", file=sys.stderr)
        raise SystemExit(2)
