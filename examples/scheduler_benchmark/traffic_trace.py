#!/usr/bin/env python3
"""Immutable offered-traffic trace for the single-cell scheduler benchmark.

Every packet any policy run will offer is written down once, before the
first run: (radio-time offset, direction, flow id, sequence number, size,
UE). The senders (radio_traffic.py) replay this file against the broker's
sample clock, so every policy is offered the same packets at the same radio
instants -- not "the same seed", which a wall-clock sender turns into
different radio instants whenever the radio runs at a different speed.

The schedule of each flow is traffic.flow_schedule() keyed by the UE's
global index (`scheduler-benchmark:<seed>:ue<i>:<flow>:traffic`), so adding
a UE does not change any other UE's packets. Payload bytes are zeros after
the header (traffic.HEADER), so the offered bytes are a pure function of the
record; the header's send timestamp is a measurement, not an input.

Files:
  traffic-trace.bin       RECORD per packet, sorted by (t, flow id, seq)
  traffic-manifest.json   SHA-256 of the file, per-flow counts/bytes, flows
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import struct
import sys
from typing import Any, Iterator, Sequence

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import definitions as bench  # noqa: E402
import traffic  # noqa: E402

TRACE_FILE = "traffic-trace.bin"
MANIFEST_FILE = "traffic-manifest.json"
FORMAT = "ocudu-traffic-trace/1"
# t_s (radio seconds from measurement sample 0), seq, flow_id, size_bytes, direction (0 dl, 1 ul), ue index
RECORD = struct.Struct("<dIHHBB")
DIRECTIONS = ("dl", "ul")


def flows_of(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per (UE, flow) with its id, port and QoS."""

    out = []
    for ue_index, ue in enumerate(profile["ues"]):
        for flow_index, flow in enumerate(ue["flows"]):
            out.append({
                "flow_id": bench.flow_id(0, ue_index, flow_index),
                "ue": f"ue{ue_index}", "ue_index": ue_index, "flow_index": flow_index,
                "name": flow["name"], "direction": flow["direction"], "pattern": flow["pattern"],
                "size_bytes": flow["size_bytes"], "five_qi": ue["five_qi"], "role": ue["role"],
                "pdb_ms": bench.FIVE_QI[ue["five_qi"]]["pdb_ms"],
                "port": bench.DL_BASE_PORT + 10 * ue_index + flow_index if flow["direction"] == "dl" else bench.UL_PORT,
                "offered_mbps": bench.offered_rate_mbps(flow), "spec": flow,
            })
    return out


def build(profile: dict[str, Any], seed: int, duration_s: float, out_dir: pathlib.Path) -> dict[str, Any]:
    if duration_s <= 0:
        raise ValueError("duration must be positive")
    out_dir.mkdir(parents=True, exist_ok=True)
    flows = flows_of(profile)
    records: list[tuple[float, int, int, int, int, int]] = []
    per_flow: dict[int, dict[str, int]] = {}
    for flow in flows:
        times = list(traffic.flow_schedule(seed, flow["ue_index"], flow["spec"], duration_s))
        direction = DIRECTIONS.index(flow["direction"])
        for seq, t in enumerate(times):
            records.append((t, seq, flow["flow_id"], flow["size_bytes"], direction, flow["ue_index"]))
        per_flow[flow["flow_id"]] = {"packets": len(times), "bytes": len(times) * flow["size_bytes"]}
    records.sort(key=lambda r: (r[0], r[2], r[1]))
    path = out_dir / TRACE_FILE
    digest = hashlib.sha256()
    with path.open("xb") as handle:
        for chunk_start in range(0, len(records), 65536):
            data = b"".join(RECORD.pack(*r) for r in records[chunk_start:chunk_start + 65536])
            digest.update(data)
            handle.write(data)
    per_ue: dict[str, dict[str, Any]] = {}
    for flow in flows:
        entry = per_ue.setdefault(flow["ue"], {"role": flow["role"], "five_qi": flow["five_qi"],
                                               "offered_mbps": {"dl": 0.0, "ul": 0.0}, "packets": 0, "bytes": 0})
        entry["offered_mbps"][flow["direction"]] += flow["offered_mbps"]
        entry["packets"] += per_flow[flow["flow_id"]]["packets"]
        entry["bytes"] += per_flow[flow["flow_id"]]["bytes"]
    manifest = {
        "format": FORMAT, "trace_file": TRACE_FILE, "trace_sha256": digest.hexdigest(),
        "record_struct": RECORD.format, "records": len(records), "seed": seed, "duration_s": duration_s,
        "profile": {key: profile[key] for key in ("name", "title", "role_rule", "ue_count", "offered_mbps")},
        "flows": [{key: value for key, value in flow.items() if key != "spec"} | {"spec": flow["spec"]}
                  for flow in flows],
        "per_flow": {str(fid): counts for fid, counts in per_flow.items()},
        "per_ue": per_ue,
        "time_base": "t_s is radio time: seconds after measurement sample 0 (S0) of the broker link that carries "
                     "the flow (gnb0>ueI for DL, ueI>gnb0 for UL); a packet is due at sample S0 + round(t_s * fs)",
        "payload": "traffic.HEADER (magic, flow id, 0, seq, send time ns) followed by zero bytes",
    }
    (out_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for name in (TRACE_FILE, MANIFEST_FILE):
        os.chmod(out_dir / name, 0o444)
    return manifest


def iter_records(bundle: pathlib.Path) -> Iterator[tuple[float, int, int, int, int, int]]:
    data = (bundle / TRACE_FILE).read_bytes()
    yield from RECORD.iter_unpack(data)


def verify(bundle: pathlib.Path) -> dict[str, Any]:
    errors = []
    try:
        manifest = json.loads((bundle / MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {"ok": False, "errors": [f"manifest unreadable: {error}"]}
    data = (bundle / TRACE_FILE).read_bytes() if (bundle / TRACE_FILE).is_file() else b""
    actual = hashlib.sha256(data).hexdigest()
    if actual != manifest.get("trace_sha256"):
        errors.append(f"traffic trace sha256 {actual} != manifest {manifest.get('trace_sha256')}")
    if len(data) % RECORD.size or len(data) // RECORD.size != manifest.get("records"):
        errors.append("traffic trace record count does not match the manifest")
    return {"ok": not errors, "errors": errors, "trace_sha256": actual}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--profile", required=True)
    b.add_argument("--ue-count", type=int, required=True)
    b.add_argument("--seed", type=int, required=True)
    b.add_argument("--duration-s", type=float, required=True)
    b.add_argument("--out-dir", type=pathlib.Path, required=True)
    v = sub.add_parser("verify")
    v.add_argument("--bundle", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "verify":
        result = verify(args.bundle)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1
    manifest = build(bench.single_cell_profile(args.profile, args.ue_count), args.seed, args.duration_s, args.out_dir)
    print(json.dumps({"event": "traffic_trace_built", "trace_sha256": manifest["trace_sha256"],
                      "records": manifest["records"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
