#!/usr/bin/env python3
"""Render native multi-UE configs from a Sionna scenario.

This is to `render-multi-ue-configs.py` what `render-sionna-rank1-configs.py`
is to the 1x1 renderer: the same gNB, Open5GS, subscriber and srsUE rendering,
but the broker topology is *generated* from the scenario instead of validating
a checked-in TDL file. That is the whole point — a Sionna run needs the broker
to carry the link ids and model names the bridge will send profile swaps for,
and those come from the scenario.

The multi-UE gate's own limits still apply, and they are the launcher's, not
this file's: one OCUDU gNB with a single ZMQ port pair, and exactly as many
single-port srsUEs as `render-multi-ue-configs.py` has UE records for, each
with its own IMSI, netns and address.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MODEL_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
# The multi-UE gNB fixture carries one unnumbered ZMQ port pair, so a scenario
# asking for an array cannot be rendered against it. The rank-1 gate is the
# multi-antenna path.
SUPPORTED_GNB_PORTS = frozenset((1,))


def load_multi_ue_renderer():
    path = Path(__file__).resolve().with_name("render-multi-ue-configs.py")
    spec = importlib.util.spec_from_file_location("render_multi_ue_configs", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load multi-UE renderer module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legacy = load_multi_ue_renderer()


@dataclass(frozen=True)
class NodePorts:
    node_id: str
    tx: int
    rx: int


@dataclass(frozen=True)
class ShapeLink:
    source: str
    destination: str
    model: str


@dataclass(frozen=True)
class LiveShape:
    gnb: NodePorts
    ues: tuple[NodePorts, ...]
    links: tuple[ShapeLink, ...]

    @property
    def nodes(self) -> tuple[NodePorts, ...]:
        return (self.gnb, *self.ues)


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    return value


def _array_count(node: dict[str, Any], key: str, where: str) -> int:
    raw = node.get(key, node.get("array"))
    array = _object({} if raw is None else raw, f"{where}.{key}")
    rows = array.get("rows", 1)
    cols = array.get("cols", 1)
    for name, value in (("rows", rows), ("cols", cols)):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{where}.{key}.{name} must be a positive integer")
    return rows * cols


def load_live_shape(path: Path) -> LiveShape:
    """Read the runtime shape from a Sionna scenario, or say why it cannot run."""

    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load Sionna scenario {path}: {error}") from error
    root = _object(root, "scenario")
    raw_nodes = _object(root.get("nodes"), "scenario.nodes")

    gnb_ids = [node_id for node_id in raw_nodes if node_id.startswith("gnb")]
    ue_ids = [node_id for node_id in raw_nodes if not node_id.startswith("gnb")]
    if len(gnb_ids) != 1:
        raise ValueError(
            "the native multi-UE gate builds one OCUDU gNB; scenario names "
            f"{len(gnb_ids)}: {', '.join(sorted(gnb_ids)) or 'none'}"
        )
    expected = [ue["device_id"] for ue in legacy.UES]
    if sorted(ue_ids) != sorted(expected):
        raise ValueError(
            "the native multi-UE gate starts one srsUE per record in "
            f"render-multi-ue-configs.py, so the scenario must name exactly "
            f"{', '.join(expected)}; it names {', '.join(sorted(ue_ids)) or 'none'}"
        )

    def ports(node_id: str) -> NodePorts:
        node = _object(raw_nodes[node_id], f"nodes.{node_id}")
        return NodePorts(
            node_id,
            _array_count(node, "tx_array", f"nodes.{node_id}"),
            _array_count(node, "rx_array", f"nodes.{node_id}"),
        )

    gnb = ports(gnb_ids[0])
    if gnb.tx not in SUPPORTED_GNB_PORTS or gnb.rx not in SUPPORTED_GNB_PORTS:
        raise ValueError(
            "the multi-UE gNB fixture carries a single ZMQ port pair, so "
            f"nodes.{gnb.node_id} must resolve to 1 port; it asks for "
            f"tx={gnb.tx}, rx={gnb.rx}. Use the rank-1 gate for an array."
        )
    ues = tuple(ports(ue["device_id"]) for ue in legacy.UES)
    for ue in ues:
        if (ue.tx, ue.rx) != (1, 1):
            raise ValueError(
                f"native srsUE is single-port; nodes.{ue.node_id} asks for "
                f"tx={ue.tx}, rx={ue.rx}"
            )

    raw_links = root.get("links")
    if not isinstance(raw_links, list) or not raw_links:
        raise ValueError("scenario.links must be a non-empty array")
    known = {port.node_id for port in (gnb, *ues)}
    links: list[ShapeLink] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw_link in enumerate(raw_links):
        link = _object(raw_link, f"links[{index}]")
        source = link.get("from")
        destination = link.get("to")
        model = link.get("model", "sionna_rt")
        if source not in known or destination not in known:
            raise ValueError(
                f"links[{index}] references a node the scenario does not "
                f"define: {source!r} -> {destination!r}"
            )
        if not isinstance(model, str) or MODEL_ID_RE.fullmatch(model) is None:
            raise ValueError(f"links[{index}].model must match {MODEL_ID_RE.pattern}")
        key = (source, destination, model)
        if key in seen:
            raise ValueError(f"duplicate link: {source}>{destination}:{model}")
        seen.add(key)
        links.append(ShapeLink(source, destination, model))

    # Each UE has to have both directions against the gNB: that pair is what
    # the RRC/PDU/ping verdict is measured on.
    for ue in ues:
        for source, destination in ((gnb.node_id, ue.node_id), (ue.node_id, gnb.node_id)):
            if not any(l.source == source and l.destination == destination for l in links):
                raise ValueError(f"scenario is missing the {source}->{destination} link")
    return LiveShape(gnb, ues, tuple(links))


def render_topology(shape: LiveShape) -> str:
    """Generate the broker topology the scenario describes.

    Endpoints follow `render-multi-ue-configs.py`'s UE table and the gNB's
    fixed 2000/2001 pair, so the ports the gate pre-checks and the srsUE
    configs use are the ones the broker binds. REP sockets bind loopback: every
    peer is in this namespace.
    """

    endpoints = {shape.gnb.node_id: (2000, 2001)}
    for ue in legacy.UES:
        endpoints[ue["device_id"]] = (ue["tx_port"], ue["rx_port"])

    # A device id may not equal a radio node id — the broker rejects the
    # config outright ("radio node id collides with a device id"). Ports carry
    # the `_p0` suffix the rank-1 topology uses, and the node keeps the bare
    # name the bridge addresses its links by.
    devices = ""
    for node in shape.nodes:
        tx_port, rx_port = endpoints[node.node_id]
        devices += (
            f"  - id: {node.node_id}_p0\n"
            "    role: port\n"
            "    sample_rate_hz: 23040000\n"
            f"    tx_endpoint: tcp://127.0.0.1:{tx_port}\n"
            f"    rx_endpoint: tcp://127.0.0.1:{rx_port}\n"
        )
    radio_nodes = "".join(
        f"  - id: {node.node_id}\n"
        "    tx_ports:\n"
        f"      - {node.node_id}_p0\n"
        "    rx_ports:\n"
        f"      - {node.node_id}_p0\n"
        for node in shape.nodes
    )
    links = "".join(
        f"  - from: {link.source}\n"
        f"    to: {link.destination}\n"
        f"    model: {link.model}\n"
        for link in shape.links
    )
    # One quiet TDL per model name. The Sionna bridge replaces each of them
    # atomically with a matrix_profile_swap before either radio starts, which
    # is why the startup taps are -100 dB rather than something plausible.
    models = "".join(
        f"  {model_id}:\n"
        "    chain:\n"
        "      - type: tdl\n"
        "        taps:\n"
        "          - delay_samples: 0.0\n"
        "            gain_db: -100.0\n"
        "            phase_rad: 0.0\n"
        for model_id in dict.fromkeys(link.model for link in shape.links)
    )
    return (
        "# Generated from the Sionna scenario by "
        "render-sionna-multi-ue-configs.py.\n"
        "runtime:\n"
        "  backend: cuda\n"
        "  gpu_device: 0\n"
        "  batch_samples: 23040\n"
        "  queue_samples: 2457600\n"
        "devices:\n"
        f"{devices}"
        "radio_nodes:\n"
        f"{radio_nodes}"
        "links:\n"
        f"{links}"
        "models:\n"
        f"{models}"
    )


def self_test() -> None:
    import tempfile

    def scenario(nodes: dict, links: list) -> str:
        return json.dumps({"name": "t", "nodes": nodes, "links": links})

    one = {"rows": 1, "cols": 1}
    good_nodes = {
        "gnb0": {"array": one},
        "ue0": {"array": one},
        "ue1": {"array": one},
    }
    good_links = [
        {"from": "gnb0", "to": "ue0", "direction": "downlink", "model": "sionna_rt"},
        {"from": "gnb0", "to": "ue1", "direction": "downlink", "model": "sionna_rt"},
        {"from": "ue0", "to": "gnb0", "direction": "uplink", "model": "sionna_rt"},
        {"from": "ue1", "to": "gnb0", "direction": "uplink", "model": "sionna_rt"},
    ]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "s.json"

        path.write_text(scenario(good_nodes, good_links), encoding="utf-8")
        shape = load_live_shape(path)
        assert [ue.node_id for ue in shape.ues] == ["ue0", "ue1"], shape.ues
        topology = render_topology(shape)
        for token in (
            "  - id: gnb0_p0\n", "  - id: ue0_p0\n", "  - id: ue1_p0\n",
            "  - id: gnb0\n", "  - id: ue0\n", "  - id: ue1\n",
            "      - ue1_p0\n",
            "    tx_endpoint: tcp://127.0.0.1:2000\n",
            "    tx_endpoint: tcp://127.0.0.1:2101\n",
            "    tx_endpoint: tcp://127.0.0.1:2103\n",
            "  sionna_rt:\n",
        ):
            assert token in topology, token
        assert topology.count("  - from: ") == 4, topology
        # Every REP socket the gate pre-checks must be bound by the broker.
        for port in (2001, 2100, 2102):
            assert f"rx_endpoint: tcp://127.0.0.1:{port}\n" in topology, port

        # A crosstalk link is carried through: the broker supports it, and the
        # scenario is the authority on which links exist.
        path.write_text(
            scenario(good_nodes, good_links
                     + [{"from": "ue0", "to": "ue1", "direction": "crosstalk",
                         "model": "sionna_rt"}]),
            encoding="utf-8",
        )
        assert render_topology(load_live_shape(path)).count("  - from: ") == 5

        for nodes, links, expected in (
            ({"gnb0": {"array": one}, "ue0": {"array": one}}, good_links,
             "must name exactly ue0, ue1"),
            ({**good_nodes, "gnb1": {"array": one}}, good_links,
             "builds one OCUDU gNB"),
            ({**good_nodes, "gnb0": {"array": {"rows": 1, "cols": 4}}}, good_links,
             "single ZMQ port pair"),
            ({**good_nodes, "ue1": {"array": {"rows": 1, "cols": 2}}}, good_links,
             "single-port"),
            (good_nodes, good_links[:3], "missing the ue1->gnb0 link"),
        ):
            path.write_text(scenario(nodes, links), encoding="utf-8")
            try:
                load_live_shape(path)
            except ValueError as error:
                assert expected in str(error), (expected, str(error))
            else:
                raise AssertionError(f"scenario should have been refused: {expected}")
    print("event=native_sionna_multi_ue_config_renderer_self_test result=pass")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--native-root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--scenario-config", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    render_args = (
        args.repo_root, args.native_root, args.output_dir, args.log_dir,
        args.scenario_config,
    )
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
    output_dir = legacy.safe_directory(args.output_dir, "output directory")
    log_dir = legacy.safe_directory(args.log_dir, "log directory")
    if (repo_root != args.repo_root or native_root != args.native_root
            or scenario != args.scenario_config):
        legacy.fail("repo, native, and scenario paths must already be canonical")

    shape = load_live_shape(scenario)
    gnb_source = legacy.read_regular(
        repo_root / "examples/ocudu/gnb_zmq_b210_fdd_srsue.yaml", "gNB fixture"
    )
    open5gs_source = legacy.read_regular(
        native_root / "src/ocudu/docker/open5gs/open5gs-5gc.yml",
        "pinned OCUDU Open5GS template",
    )
    srsue_source = legacy.read_regular(
        repo_root / "examples/native/srsran/srsue_zmq_multi_ue.conf.in",
        "native srsUE template",
    )
    subscriber_source = legacy.read_regular(
        repo_root / "examples/native/open5gs/subscriber-multi-ue.csv",
        "native subscriber template",
    )
    rendered = {
        "gnb.yaml": legacy.render_gnb(gnb_source, log_dir),
        "topology.yaml": render_topology(shape),
        "open5gs.yaml": legacy.render_open5gs(open5gs_source, native_root),
        "subscriber.csv": legacy.validate_subscriber(subscriber_source),
    }
    for ue in legacy.UES:
        rendered[f"srsue-{ue['device_id']}.conf"] = legacy.render_srsue(
            srsue_source, ue, log_dir
        )
    for name, text in rendered.items():
        if legacy.PLACEHOLDER_RE.search(text):
            legacy.fail(f"unresolved placeholder in {name}")
        legacy.write_new(output_dir / name, text)
    metadata = {
        "scenario": str(scenario),
        "gnb": shape.gnb.node_id,
        "ues": [ue.node_id for ue in shape.ues],
        "links": [
            {"from": link.source, "to": link.destination, "model": link.model}
            for link in shape.links
        ],
    }
    legacy.write_new(
        output_dir / "sionna-multi-ue-shape.json",
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
    )
    print(
        "event=native_sionna_multi_ue_configs_rendered "
        f"ues={len(shape.ues)} links={len(shape.links)} "
        f'output_dir="{output_dir}"'
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
