#!/usr/bin/env python3
"""Execute one single-cell scheduler-benchmark session.

A session is one validated configuration: shared settings (scene, gNB antennas, UE count,
UE type and placement, traffic profile, seed, durations) and two tests,
each the shared settings plus its own fields (definitions.TEST_FIELDS; today
the MAC scheduler). The runner:

  1. PREPARING   generates the scenario, the receiver-noise description, the
                 channel trace (Sionna, solved once) and the traffic trace, and
                 freezes them read-only in <session>/bundle;
  2. RUNNING     executes the tests' runs in the configured order (e.g. ABBA:
                 Test 1, Test 2, Test 2, Test 1), each a cold start of the
                 whole stack (run-single-cell-policy.sh), every run replaying
                 the SAME bundle from measurement sample 0;
  3. ANALYZING   audits the runs against each other and writes
                 <session>/reports/{fairness-audit,benchmark-report,validation}.json
                 and per-ue-kpis.csv.

Progress goes to <session>/status.json (atomically replaced) and
<session>/events.jsonl; controller.py reads both. SIGTERM stops the current
policy run's whole process group, waits for it to clean up, and records the
session as STOPPED. The bundle is never regenerated inside a session.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import signal
import subprocess
import sys
import time
from typing import Any, Sequence

HERE = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import channel_trace  # noqa: E402
import definitions as bench  # noqa: E402
import make_scenario  # noqa: E402
import traffic_trace  # noqa: E402

POLICY_SCRIPT = HERE / "run-single-cell-policy.sh"
SAMPLE_RATE_HZ = 23_040_000.0
STOP_GRACE_S = 90.0


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def write_json_atomic(path: pathlib.Path, payload: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def execution_order(policies: Sequence[str], repeat_count: int, order: str) -> list[str]:
    """Policy executions. abba: A B B A A B B A ...; alternate: A B A B; sequential: A A B B."""

    policies = list(policies)
    if repeat_count < 1:
        raise ValueError("repeat_count must be >= 1")
    if order == "sequential":
        return [p for p in policies for _ in range(repeat_count)]
    if order == "alternate" or len(policies) != 2:
        return [p for _ in range(repeat_count) for p in policies]
    if order == "abba":
        a, b = policies
        out: list[str] = []
        for index in range(repeat_count):
            out += [a, b] if index % 2 == 0 else [b, a]
        return out
    raise ValueError(f"unknown comparison_order {order!r}")


class Session:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.config = json.loads((root / "session.json").read_text(encoding="utf-8"))
        self.bundle = root / "bundle"
        self.status: dict[str, Any] = {}
        self.child: subprocess.Popen | None = None
        self.stopping = False

    # --- progress --------------------------------------------------------------
    def event(self, event: str, **fields: Any) -> None:
        with (self.root / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": event, "unix_ms": now_ms(), **fields}, separators=(",", ":")) + "\n")

    def set_status(self, state: str, message: str, **fields: Any) -> None:
        self.status.update({"session_id": self.config["session_id"], "generation": self.config["generation"],
                            "state": state, "message": message, "updated_unix_ms": now_ms(), **fields})
        write_json_atomic(self.root / "status.json", self.status)
        self.event("state", state=state, message=message)

    # --- stages ------------------------------------------------------------------
    def prepare(self) -> None:
        cfg = self.config
        self.bundle.mkdir(parents=True, exist_ok=True)
        antennas = cfg.get("gnb_antennas", 1)
        self.set_status("PREPARING", f"Preparing scene {cfg['scene_id']} with a {antennas}-antenna gNB and "
                                     f"{cfg['ue_count']} {cfg['mobility']} UEs ({cfg['placement']} placement)")
        total_s = cfg["warmup_seconds"] + cfg["measure_seconds"]
        scenario = make_scenario.generate_single_cell(cfg["seed"], cfg["scene_id"], cfg["ue_count"],
                                                      mobility=cfg["mobility"], placement=cfg["placement"],
                                                      duration_s=total_s + 60.0, gnb_antennas=antennas)
        scenario_path = self.bundle / "scenario.json"
        scenario_path.write_text(json.dumps(scenario, indent=2) + "\n", encoding="utf-8")
        noise = json.loads(subprocess.run(
            [sys.executable, str(HERE / "render_single_cell.py"), "--repo-root", str(PROJECT_ROOT), "--native-root",
             cfg["native_root"], "--output-dir", "/", "--log-dir", "/", "--scenario-config", str(scenario_path),
             "--policy", cfg["tests"][0]["scheduler"], "--ue-count", str(cfg["ue_count"]), "--traffic-profile",
             cfg["traffic_profile"], "--seed", str(cfg["seed"]), *self.noise_args(), "--noise-only"],
            capture_output=True, text=True, check=True).stdout)
        (self.bundle / "noise.json").write_text(json.dumps(noise, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        updates = int(round(total_s * cfg["channel_update_hz"]))
        self.set_status("PREPARING", f"Generating immutable channel trace ({updates} updates x "
                                     f"{2 * cfg['ue_count']} links)", trace_progress=0.0)
        env = dict(os.environ, CUDA_VISIBLE_DEVICES=os.environ.get("OCUDU_NATIVE_GPU_DEVICE", "0"))
        command = [cfg["sionna_python"], str(HERE / "channel_trace.py"), "build", "--scenario", str(scenario_path),
                   "--out-dir", str(self.bundle / "channel"), "--updates", str(updates),
                   "--update-hz", repr(float(cfg["channel_update_hz"])), "--noise-json", str(self.bundle / "noise.json")]
        with (self.root / "runtime" / "channel-trace.log").open("w", encoding="utf-8") as log:
            self.child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log, text=True, env=env,
                                          start_new_session=True)
            for line in self.child.stdout:  # type: ignore[union-attr]
                log.write(line)
                log.flush()
                if '"channel_trace_progress"' in line:
                    progress = json.loads(line)
                    self.set_status("PREPARING", "Generating immutable channel trace",
                                    trace_progress=progress["done"] / progress["total"])
            status = self.child.wait()
            self.child = None
        if self.stopping:
            raise InterruptedError
        if status != 0:
            raise RuntimeError(f"channel trace build failed (exit {status}); see runtime/channel-trace.log")
        self.set_status("PREPARING", "Generating immutable offered-traffic trace")
        traffic_trace.build(bench.single_cell_profile(cfg["traffic_profile"], cfg["ue_count"]), cfg["seed"],
                            float(total_s), self.bundle / "traffic")
        checks = {"channel": channel_trace.verify(self.bundle / "channel"),
                  "traffic": traffic_trace.verify(self.bundle / "traffic")}
        if not all(c["ok"] for c in checks.values()):
            raise RuntimeError(f"bundle verification failed: {checks}")
        for path in [self.bundle, self.bundle / "channel", self.bundle / "traffic"]:
            os.chmod(path, 0o555)
        for name in ("scenario.json", "noise.json"):
            os.chmod(self.bundle / name, 0o444)
        cmanifest = json.loads((self.bundle / "channel" / channel_trace.MANIFEST_FILE).read_text())
        tmanifest = json.loads((self.bundle / "traffic" / traffic_trace.MANIFEST_FILE).read_text())
        frozen = {"channel_trace_sha256": cmanifest["trace_sha256"], "traffic_trace_sha256": tmanifest["trace_sha256"],
                  "scenario_sha256": cmanifest["scenario_sha256"], "updates": updates,
                  "channel_timing": cmanifest["timing"], "frozen_unix_ms": now_ms()}
        write_json_atomic(self.root / "bundle-frozen.json", frozen)
        self.event("bundle_frozen", **frozen)

    def noise_args(self) -> list[str]:
        cfg = self.config
        if cfg.get("awgn_snr_db") is None:
            return []
        args = ["--awgn-snr-db", repr(float(cfg["awgn_snr_db"]))]
        if cfg.get("tx_power_ul") is not None:
            args += ["--tx-power-ul", repr(float(cfg["tx_power_ul"]))]
        if cfg.get("tx_power_dl") is not None:
            args += ["--tx-power-dl", repr(float(cfg["tx_power_dl"]))]
        return args

    def test_of(self, arm: str) -> dict[str, Any]:
        return self.config["tests"][bench.TEST_ARMS.index(arm)]

    def run_test(self, index: int, total: int, arm: str) -> dict[str, Any]:
        """One cold policy run of test `arm`; the test's fields are applied here."""

        cfg = self.config
        test = self.test_of(arm)
        policy = test["scheduler"]
        run_id = f"{index + 1:02d}-{arm}"
        run_root = self.root / "policy_runs" / run_id
        label = bench.test_label(arm, test)
        self.set_status("RUNNING_POLICY", f"Starting stack for {label} ({index + 1}/{total})",
                        arm=arm, policy=policy, policy_index=index, policy_total=total, run_id=run_id,
                        sub_phase="starting")
        command = ["bash", str(POLICY_SCRIPT), "--run-root", str(run_root), "--scenario", str(self.bundle / "scenario.json"),
                   "--channel-bundle", str(self.bundle / "channel"), "--traffic-bundle", str(self.bundle / "traffic"),
                   "--policy", policy, "--ue-count", str(cfg["ue_count"]), "--profile", cfg["traffic_profile"],
                   "--seed", str(cfg["seed"]), "--attach-timeout", str(cfg["attach_timeout_s"]),
                   "--awgn-snr-db", "off" if cfg.get("awgn_snr_db") is None else repr(float(cfg["awgn_snr_db"])),
                   "--warmup-seconds", str(cfg["warmup_seconds"])]
        if cfg.get("tx_power_ul") is not None:
            command += ["--tx-power-ul", repr(float(cfg["tx_power_ul"]))]
        if cfg.get("tx_power_dl") is not None:
            command += ["--tx-power-dl", repr(float(cfg["tx_power_dl"]))]
        if cfg.get("gnb_rx_gain_db") is not None:
            command += ["--gnb-rx-gain", repr(float(cfg["gnb_rx_gain_db"]))]
        started = now_ms()
        out_path = self.root / "runtime" / f"policy-run-{run_id}.log"
        with out_path.open("w", encoding="utf-8") as out:
            self.child = subprocess.Popen(command, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
            while self.child.poll() is None:
                self.track_sub_phase(run_root, out_path, label, index, total)
                time.sleep(1.0)
            status = self.child.returncode
            self.child = None
        result = {"run_id": run_id, "arm": arm, "test": test, "policy": policy, "exit_status": status,
                  "started_unix_ms": started, "finished_unix_ms": now_ms(), "run_root": str(run_root)}
        self.event("policy_run_finished", **result)
        return result

    def track_sub_phase(self, run_root: pathlib.Path, console: pathlib.Path, label: str, index: int,
                        total: int) -> None:
        logs = run_root / "logs"
        text = console.read_text(errors="replace") if console.exists() else ""
        if "event=measure_end" in text:
            sub, message = "draining", f"Draining {label} ({index + 1}/{total})"
        elif "event=measure_release" in text:
            sub, message = "measuring", f"Running {label} ({index + 1}/{total})"
        elif (logs / "srsue-ue0.log").exists():
            sub, message = "attaching", f"Attaching {self.config['ue_count']} UEs for {label} ({index + 1}/{total})"
        else:
            sub, message = "starting", f"Starting stack for {label} ({index + 1}/{total})"
        state = "ATTACHING_UES" if sub == "attaching" else "RUNNING_POLICY"
        if sub != self.status.get("sub_phase") or state != self.status.get("state"):
            self.set_status(state, message, sub_phase=sub)

    def run(self) -> int:
        cfg = self.config
        (self.root / "runtime").mkdir(exist_ok=True)
        (self.root / "policy_runs").mkdir(exist_ok=True)
        order = execution_order(bench.TEST_ARMS, cfg["repeat_count"], cfg["comparison_order"])
        self.status["execution_order"] = order
        self.status["test_labels"] = {arm: bench.test_label(arm, self.test_of(arm)) for arm in bench.TEST_ARMS}
        try:
            self.prepare()
            results = []
            for index, arm in enumerate(order):
                if self.stopping:
                    raise InterruptedError
                if index:
                    self.set_status("RESTARTING_STACK", f"Cold restart before {bench.test_label(arm, self.test_of(arm))} "
                                                        f"({index + 1}/{len(order)})")
                results.append(self.run_test(index, len(order), arm))
                if self.stopping:
                    raise InterruptedError
                self.status["completed_runs"] = results
            self.set_status("ANALYZING", "Validating trace, alignment, traffic and run validity")
            outcome = finalize(self.root)
            verdict = outcome["fairness"]["verdict"]
            final = "COMPLETED" if verdict.endswith("PASS") else "UNQUALIFIED"
            self.set_status(final, verdict, verdict=verdict, gates=outcome["fairness"]["gates"],
                            reasons=outcome["fairness"]["reasons"])
            return 0
        except InterruptedError:
            self.set_status("STOPPED", "Stopped by request; partial results are not a comparison")
            return 0
        except Exception as error:  # the session is FAILED, with the reason, never silently
            self.set_status("FAILED", f"{type(error).__name__}: {error}")
            return 1

    def stop(self, *_: Any) -> None:
        """SIGTERM: stop the current child's whole process group, bounded."""

        self.stopping = True
        child = self.child
        if child is None or child.poll() is not None:
            return
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            return


def collect_runs(session_root: pathlib.Path) -> list[dict[str, Any]]:
    """The session's runs in execution order: run id NN-<arm>, the test that ran
    (from session.json), and what each run recorded."""

    tests = json.loads((session_root / "session.json").read_text(encoding="utf-8")).get("tests") or []
    runs = []
    base = session_root / "policy_runs"
    for run_root in sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []:
        def read(path: pathlib.Path) -> Any:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
        arm = run_root.name.split("-", 1)[1]
        test = tests[bench.TEST_ARMS.index(arm)] if arm in bench.TEST_ARMS and len(tests) == len(bench.TEST_ARMS) else {}
        runs.append({"run_id": run_root.name, "arm": arm, "test": test, "policy": test.get("scheduler"),
                     "label": bench.test_label(arm, test) if arm in bench.TEST_ARMS else arm,
                     "report": read(run_root / "report" / "run-report.json"),
                     "gate": read(run_root / "report" / "gate-summary.json"),
                     "parameters": read(run_root / "report" / "run-parameters.json")})
    return runs


def finalize(session_root: pathlib.Path) -> dict[str, Any]:
    import validation

    config = json.loads((session_root / "session.json").read_text(encoding="utf-8"))
    return validation.write_reports(session_root / "reports", config, collect_runs(session_root),
                                    session_root / "bundle" / "channel", session_root / "bundle" / "traffic")


def preflight(native_root: pathlib.Path, build_dir: pathlib.Path, run_ctest: bool) -> dict[str, Any]:
    """Build the broker into the single-cell build directory when it is missing or older than its sources."""

    binary = build_dir / "ocudu-gpu-channel"
    sources = [p for d in ("src", "include", "apps") for p in (PROJECT_ROOT / d).rglob("*") if p.is_file()]
    sources.append(PROJECT_ROOT / "CMakeLists.txt")
    newest = max(p.stat().st_mtime for p in sources)
    stale = not binary.exists() or binary.stat().st_mtime < newest
    result: dict[str, Any] = {"binary": str(binary), "rebuilt": False, "ctest": None}
    if stale:
        nvcc = os.environ.get("CUDACXX", "/usr/local/cuda/bin/nvcc")
        subprocess.run(["cmake", "-S", str(PROJECT_ROOT), "-B", str(build_dir), "-DCMAKE_BUILD_TYPE=Release",
                        "-DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON", f"-DCMAKE_CUDA_COMPILER={nvcc}",
                        f"-DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES={os.environ.get('OCUDU_NATIVE_CUDA_ARCH', '120')}"],
                       check=True, capture_output=True)
        subprocess.run(["cmake", "--build", str(build_dir), f"-j{os.cpu_count() or 4}"], check=True, capture_output=True)
        result["rebuilt"] = True
    if run_ctest:
        done = subprocess.run(["ctest", "--test-dir", str(build_dir), "--output-on-failure"], capture_output=True,
                              text=True)
        result["ctest"] = {"status": done.returncode, "tail": done.stdout[-2000:]}
        if done.returncode != 0:
            raise RuntimeError("ctest failed in preflight")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("--session-root", type=pathlib.Path, required=True)
    f = sub.add_parser("finalize")
    f.add_argument("--session-root", type=pathlib.Path, required=True)
    c = sub.add_parser("cli", help="start a session through controller.py (same validation and reset as the UI) "
                                   "and wait for it")
    c.add_argument("--sessions-root", type=pathlib.Path, required=True)
    c.add_argument("--native-root", type=pathlib.Path, default=pathlib.Path(os.environ.get("OCUDU_NATIVE_ROOT", "")))
    c.add_argument("--sionna-python", default=os.environ.get("OCUDU_NATIVE_SIONNA_PYTHON", sys.executable))
    c.add_argument("--config", default="{}", help="JSON request, e.g. '{\"ue_count\": 8, \"placement\": \"hotspot\"}'")
    c.add_argument("--allow-unvalidated", action="store_true")
    c.add_argument("--archive", action="store_true")
    p = sub.add_parser("preflight")
    p.add_argument("--native-root", type=pathlib.Path, default=pathlib.Path(os.environ.get("OCUDU_NATIVE_ROOT", "")))
    p.add_argument("--build-dir", type=pathlib.Path)
    p.add_argument("--ctest", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "preflight":
        build_dir = args.build_dir or args.native_root / "builds/ocudu-gpu-channel-single-cell"
        print(json.dumps(preflight(args.native_root, build_dir, args.ctest), indent=2))
        return 0
    if args.command == "cli":
        import controller
        ctl = controller.Controller(args.sessions_root, args.native_root, args.sionna_python,
                                    allow_unvalidated=args.allow_unvalidated, archive=args.archive)
        try:
            started = ctl.start(json.loads(args.config))
        except controller.ValidationError as error:
            print(json.dumps({"error": "invalid configuration", "fields": error.errors}, indent=2), file=sys.stderr)
            return 2
        print(json.dumps(started), flush=True)
        last = None

        def interrupt(*_: Any) -> None:
            ctl.stop()
            raise SystemExit(130)

        signal.signal(signal.SIGINT, interrupt)
        signal.signal(signal.SIGTERM, interrupt)
        while True:
            status = ctl.status()
            line = (status.get("state"), status.get("message"))
            if line != last:
                print(f"{time.strftime('%H:%M:%S')} {line[0]}: {line[1]}", flush=True)
                last = line
            if status.get("state") in ("COMPLETED", "UNQUALIFIED", "FAILED", "STOPPED"):
                print(json.dumps({"reports": str(ctl.active_dir / "reports"), "gates": status.get("gates")}, indent=2))
                return 0 if status["state"] == "COMPLETED" else 1
            time.sleep(2.0)
    if args.command == "finalize":
        outcome = finalize(args.session_root)
        print(json.dumps({"verdict": outcome["fairness"]["verdict"], "gates": outcome["fairness"]["gates"],
                          "reasons": outcome["fairness"]["reasons"]}, indent=2))
        return 0
    session = Session(args.session_root)
    signal.signal(signal.SIGTERM, session.stop)
    signal.signal(signal.SIGINT, session.stop)
    return session.run()


if __name__ == "__main__":
    raise SystemExit(main())
