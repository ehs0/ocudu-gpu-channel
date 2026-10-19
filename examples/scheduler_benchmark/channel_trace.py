#!/usr/bin/env python3
"""Immutable channel trace: solve once, replay the same bytes to every policy.

`build` (Sionna Python, GPU) evaluates the scenario on its update grid -- the
attach channel at scenario time 0, then update k at k / update_hz for every
measured update -- and writes each update as the exact control requests the
broker will receive: one canonical JSON `matrix_profile_swap` per link
(sorted keys, no whitespace, a deterministic batch id). Nothing is solved
again afterwards; a policy run replays these strings byte for byte
(replay_bridge.py), so two runs cannot differ by a re-solve's floating-point
summation order (the legacy README measured up to 0.0038 dB of that).

Files in the bundle directory:
  channel-trace.jsonl     one record per update: {"record", "k", "t_s",
                          "positions", "link_ids", "messages", "link_sha256",
                          "batch_sha256", "summary"}; record 0 is the attach
                          channel ("attach"), then k = 0..K-1 ("update")
  channel-manifest.json   SHA-256 of the whole trace file and of every batch,
                          scenario/scene/config hashes, solver settings,
                          library versions, source revision, timing
  scene-mesh.json         scene triangles for the web UI (never replayed)

`verify` recomputes every hash from the files and fails on any mismatch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import subprocess
import sys
import time
from typing import Any, Iterator, Sequence

HERE = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
SIONNA_DIR = PROJECT_ROOT / "scripts" / "sionna_rt"
TRACE_FILE = "channel-trace.jsonl"
MANIFEST_FILE = "channel-manifest.json"
MESH_FILE = "scene-mesh.json"
FORMAT = "ocudu-channel-trace/1"
ATTACH_BATCH_ID = "attach"


def batch_id(k: int) -> str:
    return f"u{k:06d}"


def canonical(message: dict[str, Any]) -> str:
    """The one serialization a control request is ever sent in."""

    return json.dumps(message, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def batch_sha256(messages: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for message in messages:
        digest.update(message.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def source_revision() -> dict[str, Any]:
    def git(*args: str) -> str | None:
        try:
            return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], capture_output=True, text=True,
                                  timeout=10, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    status = git("status", "--porcelain", "--untracked-files=no")
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(status) if status is not None else None}


def wideband_gain_db(taps: Sequence[dict[str, Any]]) -> float | None:
    power = sum(10.0 ** (tap["gain_db"] / 10.0) for tap in taps)
    return 10.0 * math.log10(power) if power > 0 else None


ATTACH_GAIN_RULE = ("one flat tap per link: the delay of the link's first tap at scenario time 0, phase 0, and the "
                    "strongest link's wideband gain of the same direction (DL and UL separately). srsUE over ZMQ "
                    "applies no PRACH power control and a PRACH occupies ~1 MHz, so with the multipath channel the "
                    "preambles of one occasion arrive up to ~12 dB apart and the strongest masks the weakest "
                    "(near-far); the masked UE's retry RAR then stalls srsUE's RA procedure. The measurement uses "
                    "the multipath trace from update 0 on.")


def equalized_attach_profiles(profiles: dict[str, Any], tap_type: Any, lane_type: Any, matrix_type: Any) -> dict[str, Any]:
    """The attach channel (ATTACH_GAIN_RULE): flat, one tap per lane, equal
    wideband gain within a direction, capped at the control plane's +20 dB."""

    def wideband(profile: Any) -> float:
        power = sum(10.0 ** (tap.gain_db / 10.0) for lane in profile.lanes for tap in lane.taps)
        return 10.0 * math.log10(power / max(1, len(profile.lanes))) if power > 0 else -100.0

    gains = {link: wideband(profile) for link, profile in profiles.items()}
    direction = {link: ("dl" if link.startswith("gnb") else "ul") for link in profiles}
    target = {d: max(g for link, g in gains.items() if direction[link] == d) for d in set(direction.values())}
    out = {}
    for link, profile in profiles.items():
        gain = round(min(20.0, target[direction[link]]), 9)
        out[link] = matrix_type(profile.nt, profile.nr, tuple(
            lane_type(lane.rx_port, lane.tx_port,
                      (tap_type(min(tap.delay_samples for tap in lane.taps), gain, 0.0),))
            for lane in profile.lanes))
    return out


def record_for(kind: str, k: int | None, t_s: float, positions: dict[str, Any],
               profiles: dict[str, Any], make_message) -> dict[str, Any]:
    link_ids = sorted(profiles)
    bid = ATTACH_BATCH_ID if k is None else batch_id(k)
    messages = [canonical(make_message(link_id, profiles[link_id], batch_id=bid)) for link_id in link_ids]
    summary = {}
    for link_id, text in zip(link_ids, messages):
        # Over every (rx, tx) antenna lane of the link; the wideband gain is
        # the mean lane power, so a SISO link reads exactly as before.
        lanes = json.loads(text)["lanes"]
        taps = [tap for lane in lanes for tap in lane["taps"]]
        wideband = wideband_gain_db(taps)
        summary[link_id] = {"taps": max(len(lane["taps"]) for lane in lanes), "lanes": len(lanes),
                            "strongest_tap_gain_db": max(tap["gain_db"] for tap in taps),
                            "wideband_gain_db": None if wideband is None else wideband - 10.0 * math.log10(len(lanes))}
    return {"record": kind, "k": k, "t_s": t_s,
            "positions": {node: [round(float(v), 6) for v in xyz] for node, xyz in sorted(positions.items())},
            "link_ids": link_ids, "messages": messages,
            "link_sha256": {link_id: sha256_text(text) for link_id, text in zip(link_ids, messages)},
            "batch_sha256": batch_sha256(messages), "summary": summary}


def radio_materials(scenario: Any, args: Any) -> dict[str, Any]:
    """Sionna's own view of every radio material the scene uses: ITU type,
    preview colour, thickness, and relative permittivity / conductivity at the
    DL and UL carriers the trace is solved at. UI-only; never replayed."""

    def scalar(value: Any) -> float:
        try:
            return float(value[0])
        except (TypeError, IndexError):
            return float(value)

    out: dict[str, Any] = {}
    original = scenario.scene.frequency
    try:
        for label, frequency in (("dl", args.downlink_frequency_hz), ("ul", args.uplink_frequency_hz)):
            scenario.scene.frequency = frequency
            for name, material in scenario.scene.radio_materials.items():
                entry = out.setdefault(name, {
                    "itu_type": getattr(material, "itu_type", None),
                    "color_rgb": [round(float(c), 4) for c in getattr(material, "color", (0.7, 0.7, 0.7))],
                    "thickness_m": scalar(material.thickness) if hasattr(material, "thickness") else None,
                })
                entry[label] = {"frequency_hz": frequency,
                                "relative_permittivity": round(scalar(material.relative_permittivity), 4),
                                "conductivity_s_per_m": round(scalar(material.conductivity), 6)}
    finally:
        scenario.scene.frequency = original
    return out


def build(scenario_path: pathlib.Path, out_dir: pathlib.Path, *, update_hz: float, updates: int,
          sample_rate_hz: float, gain_offset_db: float, noise: dict[str, Any] | None) -> dict[str, Any]:
    sys.path.insert(0, str(SIONNA_DIR))
    import importlib.metadata

    import run_bridge
    from channel_adapter import LaneProfile, MatrixProfile, Tap, make_matrix_profile_swap

    if updates < 1 or not (math.isfinite(update_hz) and update_hz > 0):
        raise ValueError("need at least one update and a positive update rate")
    samples_per_update = sample_rate_hz / update_hz
    if abs(samples_per_update - round(samples_per_update)) > 1e-9:
        raise ValueError("sample_rate / update_hz must be an integer number of samples")
    out_dir.mkdir(parents=True, exist_ok=False)
    args = run_bridge.parse_args(["--scenario-config", str(scenario_path), "--dry-run",
                                  "--update-hz", repr(update_hz), "--sample-rate-hz", repr(sample_rate_hz),
                                  "--gain-offset-db", repr(gain_offset_db)])
    # UI-only ray polylines cost a component build per solve and never enter a
    # control message; the trace does not need them.
    args.path_polylines = 0
    started = time.monotonic()
    scenario = run_bridge.SionnaScenario(args)
    init_s = time.monotonic() - started
    mesh = dict(scenario.scene_mesh)
    mesh["radio_materials"] = radio_materials(scenario, args)
    (out_dir / MESH_FILE).write_text(json.dumps(mesh, separators=(",", ":")), encoding="utf-8")
    trace_path = out_dir / TRACE_FILE
    batch_digests = []
    solve_ms = []
    with trace_path.open("x", encoding="utf-8") as handle:
        for index in range(updates + 1):
            k = None if index == 0 else index - 1
            t_s = 0.0 if k is None else k / update_hz
            positions = scenario.update_positions(t_s)
            solve_started = time.monotonic()
            profiles, _, _ = scenario.trace_all_profiles()
            solve_ms.append((time.monotonic() - solve_started) * 1000.0)
            if k is None:
                profiles = equalized_attach_profiles(profiles, Tap, LaneProfile, MatrixProfile)
            record = record_for("attach" if k is None else "update", k, t_s, positions, profiles,
                                make_matrix_profile_swap)
            batch_digests.append(record["batch_sha256"])
            handle.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
            if index % 50 == 0:
                print(json.dumps({"event": "channel_trace_progress", "done": index + 1, "total": updates + 1}),
                      flush=True)
    scenario_bytes = scenario_path.read_bytes()
    scene_id = json.loads(scenario_bytes).get("scene")
    try:
        import scene_registry
        scene_sha = scene_registry.scene_asset_sha256(scene_id) if scene_id in scene_registry.SCENES else None
    except Exception:  # the hash is evidence, not a precondition of the build
        scene_sha = None
    config = {
        "update_hz": update_hz, "updates": updates, "sample_rate_hz": sample_rate_hz,
        "samples_per_update": int(round(samples_per_update)), "gain_offset_db": gain_offset_db,
        "downlink_frequency_hz": args.downlink_frequency_hz, "uplink_frequency_hz": args.uplink_frequency_hz,
        "solver": {"max_depth": args.max_depth, "samples_per_src": args.samples_per_src, "seed": args.seed,
                   "los": args.los, "specular_reflection": args.specular_reflection,
                   "diffuse_reflection": args.diffuse_reflection, "refraction": args.refraction,
                   "diffraction": args.diffraction},
        "noise": noise,
    }
    manifest = {
        "format": FORMAT,
        "trace_file": TRACE_FILE,
        "trace_sha256": file_sha256(trace_path),
        "records": updates + 1,
        "attach_batch_sha256": batch_digests[0],
        "update_batch_sha256": batch_digests[1:],
        "link_ids": sorted(json.loads(trace_path.open().readline())["link_ids"]),
        "scenario_sha256": hashlib.sha256(scenario_bytes).hexdigest(),
        "scene_id": scene_id,
        "scene_asset_sha256": scene_sha,
        "config": config,
        "config_sha256": sha256_text(canonical(config)),
        "noise_sha256": sha256_text(canonical(noise)) if noise is not None else None,
        "seed": args.seed,
        "attach_channel": ATTACH_GAIN_RULE,
        "versions": {name: importlib.metadata.version(name) for name in ("sionna-rt", "mitsuba", "drjit")},
        "source": source_revision(),
        "bridge_sha256": file_sha256(pathlib.Path(run_bridge.__file__)),
        "timing": {"scene_init_s": round(init_s, 3), "solve_ms_mean": round(sum(solve_ms) / len(solve_ms), 3),
                   "solve_ms_max": round(max(solve_ms), 3), "total_s": round(time.monotonic() - started, 3)},
        "environment": run_bridge.scenario_environment(args),
        "semantics": ("update k is the channel of scenario time k/update_hz; replay applies it at measurement "
                      "sample k*samples_per_update of every link (take_effect_at_sample). The attach record is "
                      "applied before the radios start and is not part of the measurement; see attach_channel."),
    }
    manifest_path = out_dir / MANIFEST_FILE
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for path in (trace_path, manifest_path, out_dir / MESH_FILE):
        os.chmod(path, 0o444)
    return manifest


def iter_records(bundle: pathlib.Path) -> Iterator[dict[str, Any]]:
    with (bundle / TRACE_FILE).open("r", encoding="utf-8") as handle:
        for line in handle:
            yield json.loads(line)


def verify(bundle: pathlib.Path) -> dict[str, Any]:
    """Recompute every hash. Returns {"ok", "errors", ...}; never raises on a mismatch."""

    errors: list[str] = []
    try:
        manifest = json.loads((bundle / MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {"ok": False, "errors": [f"manifest unreadable: {error}"]}
    if manifest.get("format") != FORMAT:
        errors.append(f"unknown trace format {manifest.get('format')!r}")
    trace_path = bundle / TRACE_FILE
    if not trace_path.is_file():
        return {"ok": False, "errors": errors + ["trace file missing"]}
    actual = file_sha256(trace_path)
    if actual != manifest.get("trace_sha256"):
        errors.append(f"trace file sha256 {actual} != manifest {manifest.get('trace_sha256')}")
    expected = [manifest.get("attach_batch_sha256")] + list(manifest.get("update_batch_sha256") or [])
    count = 0
    for index, record in enumerate(iter_records(bundle)):
        count += 1
        want_k = None if index == 0 else index - 1
        if record.get("k") != want_k:
            errors.append(f"record {index}: k={record.get('k')} expected {want_k}")
        recomputed = batch_sha256(record["messages"])
        if recomputed != record.get("batch_sha256"):
            errors.append(f"record {index}: batch digest does not match its messages")
        if index < len(expected) and recomputed != expected[index]:
            errors.append(f"record {index}: batch digest differs from the manifest")
        for link_id, text in zip(record["link_ids"], record["messages"]):
            if sha256_text(text) != record["link_sha256"].get(link_id):
                errors.append(f"record {index}: {link_id} digest mismatch")
            if canonical(json.loads(text)) != text:
                errors.append(f"record {index}: {link_id} message is not canonical JSON")
        if len(errors) > 20:
            errors.append("... (stopped after 20 errors)")
            break
    if count != manifest.get("records"):
        errors.append(f"trace has {count} records, manifest says {manifest.get('records')}")
    return {"ok": not errors, "errors": errors, "trace_sha256": actual, "records": count,
            "manifest_trace_sha256": manifest.get("trace_sha256")}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--scenario", type=pathlib.Path, required=True)
    b.add_argument("--out-dir", type=pathlib.Path, required=True)
    b.add_argument("--update-hz", type=float, default=10.0)
    b.add_argument("--updates", type=int, required=True, help="measured updates K (the attach record is extra)")
    b.add_argument("--sample-rate-hz", type=float, default=23_040_000.0)
    b.add_argument("--gain-offset-db", type=float, default=60.0)
    b.add_argument("--noise-json", type=pathlib.Path, help="receiver-noise configuration recorded in the manifest")
    v = sub.add_parser("verify")
    v.add_argument("--bundle", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "verify":
        result = verify(args.bundle)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1
    noise = json.loads(args.noise_json.read_text(encoding="utf-8")) if args.noise_json else None
    manifest = build(args.scenario, args.out_dir, update_hz=args.update_hz, updates=args.updates,
                     sample_rate_hz=args.sample_rate_hz, gain_offset_db=args.gain_offset_db, noise=noise)
    print(json.dumps({"event": "channel_trace_built", "trace_sha256": manifest["trace_sha256"],
                      "records": manifest["records"], "timing": manifest["timing"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
