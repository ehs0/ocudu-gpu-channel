#!/usr/bin/env python3
"""One-active-session controller for the single-cell scheduler benchmark.

The web server (server.py --control) delegates every state change here. At
most one session exists; a start request is handled as one transaction under
a lock:

  validate the request against what is installed and validated
      (an invalid request changes nothing: the running session keeps running)
  STOPPING_PREVIOUS  signal the active runner's process group, wait bounded,
                     then make sure no process of that session is left (every
                     process the session starts names its directory on its
                     command line) -- a session that cannot be stopped blocks
                     the new start with an error, it is never run over
  RESETTING          delete <sessions-root>/active, and only that directory
                     (or move it to archive/ when archiving is enabled)
  new session id and a monotonic generation number; session.json is written
  before the runner starts (single_cell_runner.py, its own process group)

Every response carries session_id and generation, so the UI can drop late
answers that belong to a previous session.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import channel_trace  # noqa: E402
import definitions as bench  # noqa: E402
import scene_registry  # noqa: E402

RUNNER = HERE / "single_cell_runner.py"
STOP_TIMEOUT_S = 150.0
CLEANUP_GRACE_S = 45.0
TERMINAL_STATES = ("COMPLETED", "UNQUALIFIED", "FAILED", "STOPPED")
DEFAULTS = {
    "scene_id": "sutd_1gnb", "ue_count": 4, "tests": [{"scheduler": "rr"}, {"scheduler": "qos"}],
    "traffic_profile": "mixed",
    "seed": 20261016, "measure_seconds": 120, "warmup_seconds": 10, "channel_update_hz": 10, "repeat_count": 1,
    "comparison_order": "abba", "strict_replay": True, "mobility": "static", "placement": "uniform",
}
# Radio calibration of the container's native stack (see README): absolute
# receiver floor 40 dB under a unit-gain link, the UE scale the legacy gate
# runs with, and a gNB ZMQ receive gain that keeps srsUE's +42 dB wire level
# out of int16 saturation. Not exposed in the UI; recorded in every session.
RADIO_DEFAULTS = {"awgn_snr_db": 40.0, "tx_power_ul": 300.0, "tx_power_dl": None, "gnb_rx_gain_db": -55.0}


def attach_timeout_s(ue_count: int) -> int:
    """Attach budget: every UE runs its own PRACH/RRC/PDU procedure on a radio
    slower than real time, so the budget grows with N (240 s at 8, 360 s at 16)."""

    return 120 + 15 * ue_count
LIMITS = {"measure_seconds": (10, 1800), "warmup_seconds": (0, 120), "seed": (0, 999_999_999),
          "repeat_count": (1, 4), "channel_update_hz": (1, 100)}
ORDERS = ("abba", "alternate", "sequential")


class ValidationError(ValueError):
    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


class TransitionError(RuntimeError):
    pass


def utc_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Controller:
    def __init__(self, sessions_root: pathlib.Path, native_root: pathlib.Path, sionna_python: str,
                 allow_unvalidated: bool = False, archive: bool = False) -> None:
        self.root = sessions_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.active_dir = self.root / "active"
        self.native_root = native_root
        self.sionna_python = sionna_python
        self.allow_unvalidated = allow_unvalidated
        self.archive = archive
        self.lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.runner_pid: int | None = None
        self.state_file = self.root / "controller-state.json"
        self.generation = self._load_generation()
        self.session: dict[str, Any] | None = self._recover()

    # --- persistent state ---------------------------------------------------------
    def _load_generation(self) -> int:
        try:
            return int(json.loads(self.state_file.read_text())["generation"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return 0

    def _bump_generation(self) -> int:
        self.generation += 1
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp.write_text(json.dumps({"generation": self.generation}) + "\n")
        os.replace(tmp, self.state_file)
        return self.generation

    def _recover(self) -> dict[str, Any] | None:
        """A session left by a previous server. Its runner, if still alive, is
        adopted (the experiment keeps running across a server restart);
        otherwise an unfinished session is marked STOPPED and any process it
        left behind is killed."""

        path = self.active_dir / "session.json"
        if not path.is_file():
            return None
        try:
            session = json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
        status = self._read_status(session)
        try:
            pid = int((self.active_dir / "runtime" / "runner.pid").read_text())
        except (OSError, ValueError):
            pid = None
        if status.get("state") not in TERMINAL_STATES and pid is not None and self._is_runner(pid):
            self.runner_pid = pid
            return session
        if status.get("state") not in TERMINAL_STATES:
            leftover = self._session_processes(self.active_dir)
            if leftover:
                self._kill(leftover)
            self._write_status(session, "STOPPED", "The controller restarted; this session was interrupted")
        return session

    def _read_status(self, session: dict[str, Any] | None) -> dict[str, Any]:
        if session is None:
            return {}
        try:
            return json.loads((self.active_dir / "status.json").read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_status(self, session: dict[str, Any], state: str, message: str) -> None:
        status = self._read_status(session)
        status.update({"session_id": session["session_id"], "generation": session["generation"], "state": state,
                       "message": message, "updated_unix_ms": int(time.time() * 1000)})
        tmp = self.active_dir / "status.json.tmp"
        tmp.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.active_dir / "status.json")

    # --- options and validation -----------------------------------------------------
    def options(self) -> dict[str, Any]:
        catalog = scene_registry.catalog(self.allow_unvalidated)
        return {
            **catalog,
            "policies": [{"id": key, "label": info["title"], "short": info["short"], "summary": info["summary"],
                          "properties": info["properties"], "source": info["source"]}
                         for key, info in bench.SCHEDULERS.items()],
            # Two tests, each the shared settings plus these fields (the UI draws
            # one selector per field in each test's panel).
            "tests": {"arms": [{"id": arm, "label": bench.TEST_ARM_LABELS[arm]} for arm in bench.TEST_ARMS],
                      "fields": {name: list(choices) for name, choices in bench.TEST_FIELDS.items()}},
            "traffic_profiles": [{"id": key, "label": info["title"], "role_rule": info["role_rule"]}
                                 for key, info in bench.SINGLE_CELL_PROFILES.items()],
            "comparison_orders": list(ORDERS),
            "limits": {key: list(value) for key, value in LIMITS.items()},
            "defaults": DEFAULTS,
            "radio": RADIO_DEFAULTS,
            "mobility_labels": {"static": "Static (no movement)",
                                "pedestrian": f"Pedestrian ({scene_registry.PEDESTRIAN_SPEED_MPS} m/s walk)"},
            "placement_labels": {"uniform": "Uniform", "hotspot": f"Hotspot ({scene_registry.HOTSPOT_COUNT} clusters, "
                                                                  f"σ {scene_registry.HOTSPOT_SIGMA_M:g} m)"},
            "notes": {"ue_count_1": "one UE is a control run: there is nothing to share, so fairness is not meaningful",
                      "strict_replay": "only strict replay (frozen channel and traffic traces) is implemented"},
        }

    def validate(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise ValidationError({"request": "must be a JSON object"})
        errors: dict[str, str] = {}
        cfg = dict(DEFAULTS)
        unknown = set(request) - set(DEFAULTS)
        if unknown:
            errors["request"] = f"unknown fields: {', '.join(sorted(unknown))}"
        cfg.update({k: v for k, v in request.items() if k in DEFAULTS})
        catalog = {scene["id"]: scene for scene in scene_registry.catalog(self.allow_unvalidated)["scenes"]}

        def integer(name: str) -> int | None:
            value = cfg.get(name)
            if not isinstance(value, int) or isinstance(value, bool):
                errors[name] = "must be an integer"
                return None
            low, high = LIMITS.get(name, (None, None))
            if low is not None and not low <= value <= high:
                errors[name] = f"must be in {low}..{high}"
                return None
            return value

        scene = catalog.get(cfg["scene_id"]) if isinstance(cfg["scene_id"], str) else None
        if scene is None:
            errors["scene_id"] = f"unknown scene; installed: {', '.join(catalog)}"
        elif not scene["installed"]:
            errors["scene_id"] = f"scene files missing: {', '.join(scene['missing_files'])}"
        if not isinstance(cfg["ue_count"], int) or isinstance(cfg["ue_count"], bool):
            errors["ue_count"] = "must be an integer"
        elif scene is not None and cfg["ue_count"] not in scene["startable_ue_counts"]:
            errors["ue_count"] = (f"{cfg['ue_count']} UEs is not validated for {cfg['scene_id']} "
                                  f"(startable: {scene['startable_ue_counts'] or 'none'}); see scene_validation.json")
        tests = cfg["tests"]
        if not isinstance(tests, list) or len(tests) != len(bench.TEST_ARMS):
            errors["tests"] = f"exactly {len(bench.TEST_ARMS)} tests ({', '.join(bench.TEST_ARM_LABELS.values())})"
        else:
            for arm, test in zip(bench.TEST_ARMS, tests):
                label = bench.TEST_ARM_LABELS[arm]
                if not isinstance(test, dict):
                    errors["tests"] = f"{label} must be an object"
                    continue
                unknown_fields = set(test) - set(bench.TEST_FIELDS)
                if unknown_fields:
                    errors["tests"] = f"{label}: unknown fields {', '.join(sorted(unknown_fields))}"
                for name, choices in bench.TEST_FIELDS.items():
                    if test.get(name) not in choices:
                        errors["tests"] = f"{label}: {name} must be one of {', '.join(choices)}"
        if cfg["traffic_profile"] not in bench.SINGLE_CELL_PROFILES:
            errors["traffic_profile"] = f"one of {', '.join(bench.SINGLE_CELL_PROFILES)}"
        for name in ("seed", "measure_seconds", "warmup_seconds", "repeat_count", "channel_update_hz"):
            integer(name)
        if isinstance(cfg.get("channel_update_hz"), int) and 23_040_000 % cfg["channel_update_hz"]:
            errors["channel_update_hz"] = "must divide 23.04 MS/s into whole samples"
        if cfg["comparison_order"] not in ORDERS:
            errors["comparison_order"] = f"one of {', '.join(ORDERS)}"
        if cfg["mobility"] not in scene_registry.MOBILITY_MODES:
            errors["mobility"] = f"one of {', '.join(scene_registry.MOBILITY_MODES)}"
        if cfg["placement"] not in scene_registry.PLACEMENT_MODES:
            errors["placement"] = f"one of {', '.join(scene_registry.PLACEMENT_MODES)}"
        if cfg["strict_replay"] is not True:
            errors["strict_replay"] = "only strict replay is implemented"
        if errors:
            raise ValidationError(errors)
        for path, label in ((self.native_root / "builds/ocudu-zmq-release/apps/gnb/gnb", "gNB binary"),
                            (pathlib.Path(self.sionna_python), "Sionna Python")):
            if not path.exists():
                raise ValidationError({"environment": f"{label} missing: {path}"})
        return cfg

    # --- process lifecycle ------------------------------------------------------------
    def _session_processes(self, session_dir: pathlib.Path) -> list[int]:
        """PIDs of this session, by an environment marker only: the runner is
        started with SINGLE_CELL_SESSION_DIR=<session dir>, every process below
        it inherits it, and the policy run's inner script adds
        SINGLE_CELL_RUN_ROOT=<session dir>/policy_runs/<run>. A command line
        that merely names the directory (an operator's `tail -f` of a log) is
        never matched, so it is never killed."""

        token = str(session_dir.resolve()).encode()
        markers = (b"SINGLE_CELL_SESSION_DIR=" + token + b"\0", b"SINGLE_CELL_RUN_ROOT=" + token + b"/")
        found = []
        for entry in pathlib.Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                environ = (entry / "environ").read_bytes()
            except OSError:
                continue
            if any(marker in environ for marker in markers):
                found.append(int(entry.name))
        return found

    def _is_runner(self, pid: int) -> bool:
        try:
            cmdline = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
            stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return False
        return (RUNNER.name.encode() in cmdline and str(self.active_dir).encode() in cmdline
                and stat[stat.rfind(")") + 2] != "Z")

    def _runner_alive(self) -> bool:
        if self.process is not None:
            return self.process.poll() is None
        return self.runner_pid is not None and self._is_runner(self.runner_pid)

    @staticmethod
    def _kill(pids: list[int]) -> None:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def _stop_locked(self, reason: str) -> dict[str, Any]:
        """Stop the active session's processes; returns what happened. Lock held."""

        if self.session is None:
            return {"stopped": False, "reason": "no active session"}
        if self._runner_alive():
            pgid = self.process.pid if self.process is not None else self.runner_pid
            self._write_status(self.session, "STOPPING", reason)
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + STOP_TIMEOUT_S
            while self._runner_alive() and time.monotonic() < deadline:
                time.sleep(0.25)
            if self._runner_alive():
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                time.sleep(0.5)
        if self.process is not None:
            self.process.wait(timeout=10)
        self.process = None
        self.runner_pid = None
        # The policy run's inner script stops its process groups itself
        # (bounded INT/TERM/KILL); give it that time before forcing anything.
        deadline = time.monotonic() + CLEANUP_GRACE_S
        leftover = self._session_processes(self.active_dir)
        while leftover and time.monotonic() < deadline:
            time.sleep(0.5)
            leftover = self._session_processes(self.active_dir)
        if leftover:
            self._kill(leftover)
            time.sleep(1.0)
            leftover = self._session_processes(self.active_dir)
        if leftover:
            raise TransitionError(f"processes of session {self.session['session_id']} survived SIGKILL: {leftover}")
        if self._read_status(self.session).get("state") not in TERMINAL_STATES:
            self._write_status(self.session, "STOPPED", reason)
        return {"stopped": True, "session_id": self.session["session_id"]}

    def _clear_locked(self) -> bool:
        """Delete (or archive) <root>/active and nothing else. Lock held."""

        target = self.active_dir
        if not target.exists() and not target.is_symlink():
            self.session = None
            return True
        if target.is_symlink() or target.resolve() != self.root / "active" or target.resolve().parent != self.root:
            raise TransitionError(f"refusing to clear {target}: not the allowlisted active directory")
        if self.archive and self.session is not None:
            destination = self.root / "archive" / self.session["session_id"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            suffix = 1
            while destination.exists():
                destination = self.root / "archive" / f"{self.session['session_id']}.{suffix}"
                suffix += 1
            # A frozen bundle is read-only; moving keeps it intact.
            os.replace(target, destination)
        else:
            for path in target.rglob("*"):
                if path.is_dir() and not path.is_symlink():
                    os.chmod(path, 0o755)
            shutil.rmtree(target)
        if target.exists():
            raise TransitionError(f"{target} still exists after the reset")
        self.session = None
        return True

    # --- API transitions ------------------------------------------------------------------
    def start(self, request: Any) -> dict[str, Any]:
        config = self.validate(request)  # outside the lock: an invalid request never touches the running session
        if not self.lock.acquire(timeout=1.0):
            raise TransitionError("another start/stop/reset is in progress")
        try:
            previous = self.session["session_id"] if self.session else None
            self._stop_locked("Replaced by a new benchmark")
            cleared = self._clear_locked()
            generation = self._bump_generation()
            session_id = f"{utc_stamp()}-g{generation}"
            self.active_dir.mkdir()
            for name in ("runtime", "policy_runs", "reports"):
                (self.active_dir / name).mkdir()
            session = {**config, **RADIO_DEFAULTS, "attach_timeout_s": attach_timeout_s(config["ue_count"]),
                       "session_id": session_id, "generation": generation,
                       "native_root": str(self.native_root), "sionna_python": self.sionna_python,
                       "created_unix_ms": int(time.time() * 1000), "replaced_session_id": previous,
                       "source": channel_trace.source_revision()}
            (self.active_dir / "session.json").write_text(json.dumps(session, indent=2, sort_keys=True) + "\n")
            self.session = session
            self._write_status(session, "VALIDATING", "Configuration accepted; preparing")
            log = (self.active_dir / "runtime" / "runner.log").open("w")
            self.process = subprocess.Popen([sys.executable, str(RUNNER), "run", "--session-root", str(self.active_dir)],
                                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                            cwd=str(HERE.parents[1]),
                                            env=dict(os.environ, SINGLE_CELL_SESSION_DIR=str(self.active_dir)))
            log.close()
            self.runner_pid = self.process.pid
            (self.active_dir / "runtime" / "runner.pid").write_text(str(self.process.pid))
            return {"session_id": session_id, "generation": generation, "state": "PREPARING",
                    "accepted_config": config, "old_results_cleared": cleared, "replaced_session_id": previous}
        finally:
            self.lock.release()

    def stop(self) -> dict[str, Any]:
        if not self.lock.acquire(timeout=1.0):
            raise TransitionError("another start/stop/reset is in progress")
        try:
            result = self._stop_locked("Stopped by request")
            return {**result, **self.identity()}
        finally:
            self.lock.release()

    def reset(self) -> dict[str, Any]:
        if not self.lock.acquire(timeout=1.0):
            raise TransitionError("another start/stop/reset is in progress")
        try:
            self._stop_locked("Reset by request")
            cleared = self._clear_locked()
            self._bump_generation()
            return {"session_id": None, "generation": self.generation, "state": "IDLE", "old_results_cleared": cleared}
        finally:
            self.lock.release()

    def identity(self) -> dict[str, Any]:
        return {"session_id": self.session["session_id"] if self.session else None, "generation": self.generation}

    def status(self) -> dict[str, Any]:
        if self.session is None:
            return {**self.identity(), "state": "IDLE", "message": "No benchmark", "config": None}
        status = self._read_status(self.session)
        running = self._runner_alive()
        state = status.get("state", "VALIDATING")
        if not running and state not in TERMINAL_STATES and (self.process is not None or self.runner_pid):
            state, status["message"] = "FAILED", "The runner exited without a final state (runtime/runner.log)"
            self._write_status(self.session, state, status["message"])
        config = {k: self.session.get(k) for k in list(DEFAULTS) + list(RADIO_DEFAULTS)}
        return {**status, **self.identity(), "state": state, "runner_alive": running, "config": config}


class Results:
    """Read-side view of the active session for the UI: live per-run analyses
    (refreshed at most every REFRESH_S), the final reports, and the frozen
    trace's channel and position timeline. Everything is keyed to the
    session it was computed for; a new session starts from empty caches."""

    REFRESH_S = 2.0

    def __init__(self, controller: Controller) -> None:
        self.controller = controller
        self.lock = threading.Lock()
        self.session_id: str | None = None
        self.analyzers: dict[str, Any] = {}
        self.cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.timeline: dict[str, Any] | None = None

    def _reset_if_new(self) -> None:
        current = self.controller.identity()["session_id"]
        if current != self.session_id:
            self.session_id = current
            self.analyzers.clear()
            self.cache.clear()
            self.timeline = None

    def _run_report(self, run_root: pathlib.Path, live: bool, warmup: int) -> dict[str, Any] | None:
        import single_cell_analyze

        bundle = self.controller.active_dir / "bundle"
        saved = run_root / "report" / "run-report.json"
        if not live and saved.is_file():
            try:
                return json.loads(saved.read_text())
            except json.JSONDecodeError:
                pass
        if not (run_root / "configs" / "benchmark.json").is_file():
            return None
        key = run_root.name
        stamp, report = self.cache.get(key, (0.0, None))
        if report is not None and time.monotonic() - stamp < self.REFRESH_S:
            return report
        analyzer = self.analyzers.get(key)
        if analyzer is None:
            analyzer = single_cell_analyze.SingleCellRun(run_root, bundle / "channel", bundle / "traffic", warmup)
            self.analyzers[key] = analyzer
        analyzer.poll()
        report = analyzer.report(live=live)
        report["resource_grid"] = analyzer.resource_grid()
        self.cache[key] = (time.monotonic(), report)
        return report

    @staticmethod
    def _slim(report: dict[str, Any] | None) -> dict[str, Any] | None:
        if report is None:
            return None
        keep = ("policy", "ue_count", "live", "measure_seconds", "warmup_seconds", "radio_seconds_total",
                "segments_complete", "segments_valid", "metrics", "runtime", "epoch", "resource_grid")
        out = {k: report.get(k) for k in keep}
        out["segments"] = [{"k": s["k"], "valid": s["valid"], "reasons": s["reasons"][:2], "dl_mbps": s["dl_mbps"],
                            "jain_dl": s["jain_dl"], "dl_prb_util": s["dl_prb_util"],
                            "ues": {u: {"dl_mbps": v["dl_mbps"], "dl_prb_share": v["dl_prb_share"]}
                                    for u, v in s["ues"].items()}} for s in report.get("segments") or []]
        evidence = report.get("evidence") or {}
        applied = evidence.get("applied") or {}
        out["evidence"] = {
            "links": {link: {k: info[k] for k in ("updates", "exact", "relative_ok", "missing_count", "monotonic")}
                      | {"attach": info.get("attach") is not None} for link, info in applied.items()},
            "noise_epochs": evidence.get("noise_epochs"), "noise_missed": evidence.get("noise_missed"),
            "traffic": {k: v for k, v in (evidence.get("traffic") or {}).items() if k != "sender_reports"},
        }
        return out

    def results(self) -> dict[str, Any]:
        with self.lock:
            self._reset_if_new()
            identity = self.controller.identity()
            if self.controller.session is None:
                return {**identity, "runs": [], "reports": None}
            status = self.controller.status()
            active_run = status.get("run_id") if status.get("state") not in TERMINAL_STATES else None
            warmup = int(self.controller.session.get("warmup_seconds") or 0)
            runs = []
            base = self.controller.active_dir / "policy_runs"
            for run_root in sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []:
                live = run_root.name == active_run
                try:
                    report = self._run_report(run_root, live, warmup)
                except Exception as error:  # a half-written log must not break the page
                    report = {"error": f"{type(error).__name__}: {error}"}
                gate = None
                try:
                    gate = json.loads((run_root / "report" / "gate-summary.json").read_text())
                except (OSError, json.JSONDecodeError):
                    pass
                arm = run_root.name.split("-", 1)[1]
                tests = self.controller.session.get("tests") or []
                test = tests[bench.TEST_ARMS.index(arm)] if arm in bench.TEST_ARMS and len(tests) == 2 else {}
                runs.append({"run_id": run_root.name, "arm": arm, "test": test, "policy": test.get("scheduler"),
                             "label": bench.test_label(arm, test) if test else arm, "live": live,
                             "gate": gate and {k: gate.get(k) for k in ("status", "measure_status", "all_ues_attached",
                                                                         "broker_clean", "per_ue")},
                             "report": self._slim(report) if report and "error" not in report else report})
            reports = None
            reports_dir = self.controller.active_dir / "reports"
            if (reports_dir / "fairness-audit.json").is_file():
                reports = {}
                for name in ("fairness-audit", "benchmark-report"):
                    try:
                        reports[name] = json.loads((reports_dir / f"{name}.json").read_text())
                    except (OSError, json.JSONDecodeError):
                        reports[name] = None
            frozen = None
            try:
                frozen = json.loads((self.controller.active_dir / "bundle-frozen.json").read_text())
            except (OSError, json.JSONDecodeError):
                pass
            return {**identity, "state": status.get("state"), "runs": runs, "reports": reports, "bundle": frozen}

    def _load_timeline(self) -> dict[str, Any] | None:
        bundle = self.controller.active_dir / "bundle" / "channel"
        if not (bundle / channel_trace.MANIFEST_FILE).is_file():
            return None
        manifest = json.loads((bundle / channel_trace.MANIFEST_FILE).read_text())
        hz = manifest["config"]["update_hz"]
        per_second: dict[int, dict[str, Any]] = {}
        for record in channel_trace.iter_records(bundle):
            if record["k"] is None:
                continue
            second = int(record["k"] // hz)
            slot = per_second.setdefault(second, {"positions": record["positions"], "gain": {}})
            for link_id, summary in record["summary"].items():
                slot["gain"].setdefault(link_id, []).append(summary["wideband_gain_db"])
        seconds = []
        for second in sorted(per_second):
            entry = per_second[second]
            seconds.append({"t": second, "positions": entry["positions"],
                            "gain_db": {link: round(sum(v for v in values if v is not None) / max(1, len(values)), 3)
                                        for link, values in entry["gain"].items()}})
        return {"trace_sha256": manifest["trace_sha256"], "update_hz": hz, "seconds": seconds}

    def scene(self) -> dict[str, Any]:
        with self.lock:
            self._reset_if_new()
            identity = self.controller.identity()
            if self.controller.session is None:
                return {**identity, "available": False}
            if self.timeline is None:
                self.timeline = self._load_timeline()
            if self.timeline is None:
                return {**identity, "available": False, "reason": "the scene bundle is being generated"}
            scenario = json.loads((self.controller.active_dir / "bundle" / "scenario.json").read_text())
            bench_meta = scenario.get("benchmark") or {}
            return {**identity, "available": True, "scene_id": self.controller.session["scene_id"],
                    "mobility": bench_meta.get("mobility"), "placement": bench_meta.get("placement"),
                    "hotspots": bench_meta.get("hotspots"), "gnb": scenario["nodes"]["gnb0"].get("start_m"),
                    "ues": sorted((n for n in scenario["nodes"] if n != "gnb0"), key=lambda n: int(n[2:])), "timeline": self.timeline}

    def mesh_path(self) -> pathlib.Path | None:
        path = self.controller.active_dir / "bundle" / "channel" / channel_trace.MESH_FILE
        return path if path.is_file() else None
