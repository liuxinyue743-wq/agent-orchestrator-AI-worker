#!/usr/bin/env python
"""CLAO v0.2 one-command Mission runner.

Usage (from the CLAO product directory):
    PYTHONPATH=src .venv/Scripts/python.exe run_mission.py tasks/mission-quick.json
    ... add --dry-run to preflight Planner decomposition without touching AO.

Wires: config -> AO daemon -> MissionController (Planner/Auditor/Verifier via
Codex CLI). Workers are AO Chat-mode Codex workers using the configured model
(default gpt-5.6-sol).
Observer/Gate use no model) -> LoopBus projection -> memory.md / project.md
-> FINAL_REPORT.

The same wiring is importable (build_runtime / run_loop) so the web panel
drives the EXACT code path this CLI validates — no second implementation.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from loopcore.action_executor import ActionExecutor          # noqa: E402
from loopcore.ao_adapter import AOAdapter                    # noqa: E402
from loopcore.auditor import CodexCliAuditorProvider         # noqa: E402
from loopcore.bus import BusConfig, LoopBus                  # noqa: E402
from loopcore.bus_projector import StoreBusProjector         # noqa: E402
from loopcore.memory import ProjectMemory                    # noqa: E402
from loopcore.mission import (MISSION_TERMINAL, MissionController,
                              deterministic_single_task_plan)  # noqa: E402
from loopcore.mission_contracts import (MissionSpec,
                                        new_mission_max_subtasks)  # noqa: E402
from loopcore.mission_gate import IntegrationGate            # noqa: E402
from loopcore.planner_adapter import CodexCliPlannerProvider       # noqa: E402
from loopcore.state_store import StateStore                  # noqa: E402
from loopcore.verifier import CodexCliVerifierProvider       # noqa: E402


SAMPLE_PROJECT_PLACEHOLDER = "REPLACE_WITH_AO_PROJECT_ID"


class PreflightError(RuntimeError):
    """A bounded, user-actionable Mission environment failure."""


def resolve_ao_bin(*, environ=None, which=None) -> str:
    """Resolve the external AO CLI without assuming a machine layout."""
    env = os.environ if environ is None else environ
    configured = str(env.get("CLAO_AO_BIN") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise RuntimeError(
                "CLAO_AO_BIN does not point to an AO executable file: %s"
                % configured)
        return str(path)

    finder = shutil.which if which is None else which
    discovered = finder("ao")
    if discovered:
        return str(discovered)
    raise RuntimeError(
        "AO executable not found; set CLAO_AO_BIN to the installed AO CLI "
        "or make 'ao' available on PATH")


def resolve_ao_run_file(*, environ=None, home=None) -> Path:
    """Resolve AO's daemon runfile from the portable public contract."""
    env = os.environ if environ is None else environ
    configured = str(env.get("CLAO_AO_RUN_FILE") or "").strip()
    if configured:
        return Path(configured).expanduser()
    home_dir = Path.home() if home is None else Path(home)
    return home_dir / ".ao" / "running.json"


def setup_environment(*, ao_run_file: Path | str | None = None) -> None:
    """Process-level env every entry point needs (CLI, panel, scripts).

    AO Desktop remains an external dependency and is never started here.  A
    resolved runfile may be published for compatibility with AO consumers;
    no developer-specific AO data directory is injected.  Codex role
    providers do not use llm_env.
    """
    if ao_run_file is not None:
        os.environ["AO_RUN_FILE"] = str(ao_run_file)
    # the mission gate runs `python -m pytest` argv-style: make sure the
    # venv python (with pytest) wins PATH resolution.
    venv_scripts = str(ROOT / ".venv" / "Scripts")
    if venv_scripts not in os.environ.get("PATH", ""):
        os.environ["PATH"] = venv_scripts + os.pathsep + \
            os.environ.get("PATH", "")


def load_config() -> dict:
    return yaml.safe_load(
        (ROOT / "config" / "default.yaml").read_text("utf-8"))


def _run_preflight_command(argv: list[str], *, cwd: Path | None = None):
    """Run one read-only capability probe without a shell."""
    try:
        return subprocess.run(
            argv, cwd=str(cwd) if cwd is not None else None,
            capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreflightError("command probe failed: %s" % argv[0]) from exc


_AO_ORIGIN_REQUIRED = (
    "AO Project has no origin remote required by the validated AO 0.12.9 "
    "workspace contract."
)


def _workspace_ref_error(ref: str) -> PreflightError:
    display_ref = ref if len(ref) <= 160 else ref[:157] + "..."
    return PreflightError(
        "AO Project is not workspace-ready for AO 0.12.9: remote-backed "
        "base %s is unavailable. Fetch/configure origin before running "
        "CLAO." % display_ref)


def _validate_project_default_branch(
        git_bin: str, project_path: Path, project_detail: dict) -> str:
    """Validate AO 0.12.9's observed remote-backed workspace contract."""
    configured = str(project_detail.get("defaultBranch") or "auto").strip()
    remote_result = _run_preflight_command(
        [git_bin, "remote"], cwd=project_path)
    if remote_result.returncode != 0:
        raise PreflightError(_AO_ORIGIN_REQUIRED)
    remotes = [line.strip() for line in remote_result.stdout.splitlines()
               if line.strip()]
    if "origin" not in remotes:
        raise PreflightError(_AO_ORIGIN_REQUIRED)

    if configured.lower() == "auto":
        origin_head = "refs/remotes/origin/HEAD"
        symbolic = _run_preflight_command(
            [git_bin, "symbolic-ref", "--quiet", origin_head],
            cwd=project_path)
        target_ref = symbolic.stdout.strip() if symbolic.returncode == 0 \
            else ""
        prefix = "refs/remotes/origin/"
        if not target_ref.startswith(prefix) or target_ref == origin_head:
            raise _workspace_ref_error(origin_head)
        branch = target_ref[len(prefix):]
    else:
        branch = configured
        target_ref = "refs/remotes/origin/" + branch

    exists = _run_preflight_command(
        [git_bin, "rev-parse", "--verify", "--quiet",
         target_ref + "^{commit}"], cwd=project_path)
    if exists.returncode != 0:
        raise _workspace_ref_error(target_ref)
    return branch


def mission_preflight(mission_dict: dict, cfg: dict) -> dict:
    """Validate shared CLI/Panel Mission prerequisites before runtime state.

    This is deliberately capability-based and read-only: it never installs
    tools, writes Git configuration, creates runtime state, or calls a model.
    """
    if platform.python_implementation() != "CPython" \
            or sys.version_info[:2] != (3, 12):
        raise PreflightError("CPython 3.12.x is required")

    git_bin = shutil.which("git")
    if not git_bin:
        raise PreflightError("Git executable not found on PATH")

    project_id = str(mission_dict.get("project_id") or "").strip()
    if project_id == SAMPLE_PROJECT_PLACEHOLDER:
        raise PreflightError(
            "replace sample project_id with a registered AO Project id")
    if not project_id:
        raise PreflightError("mission project_id is required")

    try:
        ao_bin = resolve_ao_bin()
    except RuntimeError as exc:
        raise PreflightError(str(exc)) from exc
    ao_run_file = resolve_ao_run_file()
    if not ao_run_file.is_file():
        raise PreflightError(
            "AO daemon runfile not found: set CLAO_AO_RUN_FILE or start AO")

    ao_cfg = cfg.get("ao") or {}
    adapter = AOAdapter(
        base_url=ao_cfg.get("base_url") or "http://127.0.0.1:3001",
        timeout=float(ao_cfg.get("request_timeout_seconds", 15)),
        run_file=ao_run_file)
    try:
        projects = adapter.get_projects()
    except Exception as exc:
        raise PreflightError("AO daemon/API unavailable") from exc
    project = next(
        (item for item in projects
         if isinstance(item, dict) and str(item.get("id")) == project_id),
        None)
    if project is None:
        raise PreflightError("AO Project is not registered: %s" % project_id)
    project_text = str(project.get("path") or "").strip()
    project_path = Path(project_text) if project_text else None
    if project_path is None or not project_path.is_dir():
        raise PreflightError("AO Project path is unavailable: %s" % project_id)
    try:
        project_detail = adapter.get_project(project_id)
    except Exception as exc:
        raise PreflightError("AO Project detail is unavailable: %s"
                             % project_id) from exc

    worktree = _run_preflight_command(
        [git_bin, "rev-parse", "--is-inside-work-tree"], cwd=project_path)
    if worktree.returncode != 0 or worktree.stdout.strip().lower() != "true":
        raise PreflightError("selected AO Project path is not a Git worktree")
    for key in ("user.name", "user.email"):
        identity = _run_preflight_command(
            [git_bin, "config", "--get", key], cwd=project_path)
        if identity.returncode != 0 or not identity.stdout.strip():
            raise PreflightError("Git identity is missing: %s" % key)
    _validate_project_default_branch(
        git_bin, project_path, project_detail)

    codex_bin = shutil.which("codex")
    if not codex_bin:
        raise PreflightError("Codex CLI executable not found on PATH")
    login = _run_preflight_command([codex_bin, "login", "status"])
    login_text = "%s\n%s" % (login.stdout or "", login.stderr or "")
    if login.returncode != 0 or "chatgpt" not in login_text.lower():
        raise PreflightError("Codex CLI is not logged in with ChatGPT")

    roles = cfg.get("roles") or {}
    required_models = {
        "roles.planner.model": ((roles.get("planner") or {}).get("model")),
        "roles.auditor.model": ((roles.get("auditor") or {}).get("model")),
        "roles.verifier.model": ((roles.get("verifier") or {}).get("model")),
        "worker.model": ((cfg.get("worker") or {}).get("model")),
    }
    missing = [name for name, value in required_models.items()
               if not str(value or "").strip()]
    if missing:
        raise PreflightError("model configuration is missing: %s" % missing[0])

    return {
        "ao_bin": ao_bin,
        "ao_run_file": ao_run_file,
        "project_path": project_path,
    }


def build_planner(cfg: dict, *, timeout: int = 180,
                  codex_bin: str = "codex",
                  cwd: Path | None = None) -> CodexCliPlannerProvider:
    """Build the one production Planner used by normal and dry-run paths."""
    roles = cfg.get("roles") or {}
    planner_cfg = roles.get("planner") or {}
    model = planner_cfg.get("model") or "gpt-5.6-sol"
    return CodexCliPlannerProvider(
        model=model,
        timeout=timeout,
        codex_bin=codex_bin,
        cwd=cwd or ROOT,
    )


class MissionRuntime:
    """Everything a running (or resumable) mission is made of."""

    def __init__(self, mission_dict: dict, cfg: dict, *, ao_bin: str,
                 ao_run_file: Path, dry_run: bool = False):
        self.mission_dict = mission_dict
        self.cfg = cfg
        self.dry_run = dry_run
        self.ao_bin = ao_bin
        self.ao_run_file = str(ao_run_file)
        self.runtime = ROOT / "runtime" / mission_dict["mission_id"]
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.store = StateStore(str(self.runtime / "state.db"))
        ao_cfg = cfg.get("ao") or {}
        self.adapter = AOAdapter(
            base_url=ao_cfg.get("base_url") or "http://127.0.0.1:3001",
            timeout=float(ao_cfg.get("request_timeout_seconds", 15)),
            run_file=ao_run_file)
        self.ao_base_url = self.adapter.base_url
        wcfg = cfg.get("worker") or {}
        self.executor = ActionExecutor(
            ao_bin=ao_bin, data_dir=None, run_file=str(ao_run_file),
            store=self.store,
            worker_model=wcfg.get("model", ""),
            max_spawn_attempts=int(wcfg.get("spawn_max_attempts", 3)),
            spawn_backoff_seconds=int(wcfg.get("spawn_backoff_seconds", 30)),
            max_transient_spawn_attempts=int(
                wcfg.get("spawn_max_transient_attempts", 8)),
            transient_spawn_backoff_seconds=int(
                wcfg.get("spawn_transient_backoff_seconds", 90)))
        self.gate = IntegrationGate(self.store)
        planner = build_planner(cfg, timeout=180, cwd=ROOT)
        roles = cfg.get("roles") or {}
        auditor_cfg = roles.get("auditor") or {}
        verifier_cfg = roles.get("verifier") or {}
        auditor = CodexCliAuditorProvider(
            model=auditor_cfg.get("model") or "gpt-5.6-sol",
            timeout=180, cwd=ROOT)
        verifier = CodexCliVerifierProvider(
            model=verifier_cfg.get("model") or "gpt-5.6-sol",
            timeout=180, cwd=ROOT)
        # Keep references for lifecycle introspection and compatibility with
        # any provider that exposes optional cleanup.
        self._planner = planner
        self._auditor = auditor
        self._verifier = verifier
        self.mission = MissionSpec.from_dict(mission_dict)
        self.controller = MissionController(
            self.mission, cfg,
            planner=planner, auditor=auditor, verifier=verifier,
            executor=self.executor, adapter=self.adapter, gate=self.gate,
            store=self.store, dry_run=dry_run)
        bus_cfg = cfg.get("bus") or {}
        self.bus = LoopBus(BusConfig(
            max_hops_per_thread=int(bus_cfg.get("max_hops_per_thread", 24)),
            max_audits_per_thread=int(bus_cfg.get("max_audits_per_thread", 3)),
            overall_timeout_seconds=float(
                bus_cfg.get("overall_timeout_seconds", 600))))
        # run memory lands in runtime/, never pollutes the target repo
        self.memory = ProjectMemory(str(self.runtime))
        self.projector = StoreBusProjector(
            self.store, self.bus, self.memory,
            traffic_log=self.runtime / "bus_traffic.jsonl")

    def close(self) -> None:
        """Release optional provider resources and sqlite. Idempotent.
        The panel calls this when a mission is unloaded; the CLI path relies
        on process exit, but close() keeps long-running panel use leak-free."""
        for prov in (self._planner, self._auditor, self._verifier):
            fn = getattr(prov, "close", None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
        try:
            self.store.close()
        except Exception:
            pass


def build_runtime(mission_dict: dict, cfg: dict, *, dry_run: bool = False,
                  require_ao: bool = True) -> MissionRuntime:
    """Build a runtime; only read-only inspection may omit AO discovery."""
    if not require_ao and not dry_run:
        raise ValueError(
            "require_ao=False is only valid for read-only inspection")
    if require_ao:
        checked = mission_preflight(mission_dict, cfg)
        ao_bin = checked["ao_bin"]
        ao_run_file = checked["ao_run_file"]
    else:
        ao_bin = "ao-unavailable-read-only"
        ao_run_file = resolve_ao_run_file()
    setup_environment(ao_run_file=ao_run_file if require_ao else None)
    return MissionRuntime(
        mission_dict, cfg, ao_bin=ao_bin, ao_run_file=ao_run_file,
        dry_run=dry_run)


def run_loop(rt: MissionRuntime, *, cap_seconds: float = 300.0,
             poll_seconds: float = 5.0, on_tick=None,
             should_stop=None) -> dict:
    """Drive the controller until terminal / cap / external stop.

    on_tick(result, projected_n, elapsed) fires every iteration (the panel
    uses it for heartbeats); should_stop() lets the panel abort without
    killing the thread (state stays resumable in the store).
    """
    started = time.monotonic()
    while True:
        result = rt.controller.step()
        n = rt.projector.project_once()
        state = result.get("state", "?")
        elapsed = time.monotonic() - started
        if on_tick:
            try:
                on_tick(result, n, elapsed)
            except Exception:
                pass
        if state in MISSION_TERMINAL:
            break
        if elapsed >= cap_seconds:
            break
        if should_stop and should_stop():
            break
        time.sleep(poll_seconds)
    rt.projector.project_once()
    return {
        "mission_id": rt.mission.mission_id,
        "final_state": rt.controller.state,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "bus_envelopes": len(rt.projector.projected),
        "bus_errors": rt.projector.errors,
        "runtime_dir": str(rt.runtime),
        "memory_md": str(rt.memory.memory_path),
        "project_md": str(rt.memory.project_path),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mission_json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--poll-seconds", type=float, default=5.0)
    ap.add_argument("--cap-seconds", type=float, default=300.0,
                    help="hard wall-clock cap for this runner (default 5 min)")
    args = ap.parse_args()

    try:
        mission_dict = json.loads(Path(args.mission_json).read_text("utf-8"))
    except Exception as exc:
        if args.dry_run:
            print("planning dry-run error: invalid mission JSON: %s" % exc,
                  file=sys.stderr)
            return 2
        raise
    cfg = load_config()

    if args.dry_run:
        try:
            if not isinstance(mission_dict, dict):
                raise ValueError("mission must be a JSON object")
            for field in ("allowed_paths", "forbidden_paths",
                          "acceptance_criteria", "gate_commands"):
                if not isinstance(mission_dict.get(field), list):
                    raise ValueError("%s must be a list" % field)
            if not isinstance(mission_dict.get("budgets", {}), dict):
                raise ValueError("budgets must be an object")
            if not all(isinstance(path, str)
                       for path in mission_dict["allowed_paths"]
                       + mission_dict["forbidden_paths"]):
                raise ValueError("mission paths must be strings")
            if not all(isinstance(command, str)
                       for command in mission_dict["gate_commands"]):
                raise ValueError("gate_commands entries must be strings")
            if not all(isinstance(item, dict) and item.get("id")
                       and item.get("description")
                       for item in mission_dict["acceptance_criteria"]):
                raise ValueError(
                    "acceptance criteria require id and description")
            mission = MissionSpec.from_dict(mission_dict)
            if not mission.mission_id or not mission.project_id \
                    or not mission.objective:
                raise ValueError(
                    "mission_id, project_id, and objective are required")
            if not mission.allowed_paths:
                raise ValueError("allowed_paths must be non-empty")
            if not mission.acceptance_criteria:
                raise ValueError("acceptance_criteria must be non-empty")
            max_subtasks = new_mission_max_subtasks(mission.budgets)
            if max_subtasks == 1:
                planner = None
                plan = deterministic_single_task_plan(mission)
            else:
                planner = build_planner(cfg, timeout=180, cwd=ROOT)
                plan = planner.plan_decompose(
                    mission.to_dict(), "DECOMP-%s" % mission.mission_id)
            summary = {
                "mission_id": mission.mission_id,
                "dry_run": True,
                "planner_provider": (type(planner).__name__
                                     if planner is not None else None),
                "model": planner.model if planner is not None else None,
                "subtask_count": len(plan.subtasks),
                "plan": plan.to_dict(),
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2),
                  flush=True)
            return 0
        except Exception as exc:
            detail = str(exc).replace("\r", " ").replace("\n", " ")[:400]
            print("planning dry-run error: %s" % detail, file=sys.stderr)
            return 2

    setup_environment()
    try:
        rt = build_runtime(mission_dict, cfg, dry_run=False)
    except PreflightError as exc:
        detail = str(exc).replace("\r", " ").replace("\n", " ")[:400]
        print("preflight failed: %s" % detail, file=sys.stderr)
        return 2

    print(f"[runner] mission={rt.mission.mission_id} "
          f"project={rt.mission.project_id} dry_run={args.dry_run} "
          f"cap={args.cap_seconds:g}s", flush=True)

    def _tick(result, n, elapsed):
        print(f"[runner] {elapsed:6.1f}s state={result.get('state', '?')} "
              f"acted={result.get('acted')} bus+{n}", flush=True)

    summary = run_loop(rt, cap_seconds=args.cap_seconds,
                       poll_seconds=args.poll_seconds, on_tick=_tick)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["final_state"] == "MISSION_DONE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
