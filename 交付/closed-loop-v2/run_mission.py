#!/usr/bin/env python
"""closed-loop-v2 one-command mission runner.

Usage (from the closed-loop-v2 directory):
    PYTHONPATH=src .venv/Scripts/python.exe run_mission.py tasks/mission-quick.json
    ... add --dry-run to exercise the pipeline without touching AO.

Wires: config -> AO daemon -> MissionController (Planner/Auditor/Verifier via
headless claude CLI, Workers via ao spawn) -> LoopBus projection ->
memory.md / project.md -> FINAL_REPORT.

The same wiring is importable (build_runtime / run_loop) so the web panel
drives the EXACT code path this CLI validates — no second implementation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from loopcore.action_executor import ActionExecutor          # noqa: E402
from loopcore.ao_adapter import AOAdapter                    # noqa: E402
from loopcore.auditor import ClaudeCliAuditorProvider        # noqa: E402
from loopcore.bus import BusConfig, LoopBus                  # noqa: E402
from loopcore.bus_projector import StoreBusProjector         # noqa: E402
from loopcore.memory import ProjectMemory                    # noqa: E402
from loopcore.mission import MISSION_TERMINAL, MissionController  # noqa: E402
from loopcore.mission_contracts import MissionSpec           # noqa: E402
from loopcore.mission_gate import IntegrationGate            # noqa: E402
from loopcore.planner_adapter import AOOrchestratorPlannerProvider  # noqa: E402
from loopcore.state_store import StateStore                  # noqa: E402
from loopcore.verifier import ClaudeCliVerifierProvider      # noqa: E402

AO_BIN = r"E:\智理杯智能体大赛\ao-app\resources\daemon\ao.exe"
AO_DATA_DIR = r"E:\智理杯智能体大赛\ao-data"
AO_RUN_FILE = str(Path(AO_DATA_DIR) / "ao.run")


def setup_environment() -> None:
    """Process-level env every entry point needs (CLI, panel, scripts).

    Provider-level CLAUDE_CODE_GIT_BASH_PATH / ANTHROPIC_MODEL defaults now
    live in loopcore.llm_env (self-sufficient providers); what remains here
    is AO daemon discovery and the venv-first PATH for the integration gate.
    """
    os.environ.setdefault("AO_DATA_DIR", AO_DATA_DIR)
    os.environ.setdefault("AO_RUN_FILE", AO_RUN_FILE)
    from loopcore.llm_env import ensure_llm_env
    ensure_llm_env()
    # the mission gate runs `python -m pytest` argv-style: make sure the
    # venv python (with pytest) wins PATH resolution.
    venv_scripts = str(ROOT / ".venv" / "Scripts")
    if venv_scripts not in os.environ.get("PATH", ""):
        os.environ["PATH"] = venv_scripts + os.pathsep + \
            os.environ.get("PATH", "")


def load_config() -> dict:
    return yaml.safe_load(
        (ROOT / "config" / "default.yaml").read_text("utf-8"))


class MissionRuntime:
    """Everything a running (or resumable) mission is made of."""

    def __init__(self, mission_dict: dict, cfg: dict, *, dry_run: bool = False):
        self.mission_dict = mission_dict
        self.cfg = cfg
        self.dry_run = dry_run
        self.runtime = ROOT / "runtime" / mission_dict["mission_id"]
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.store = StateStore(str(self.runtime / "state.db"))
        self.adapter = AOAdapter()
        self.executor = ActionExecutor(
            ao_bin=AO_BIN, data_dir=AO_DATA_DIR, run_file=AO_RUN_FILE,
            store=self.store,
            worker_model=(cfg.get("worker") or {}).get("model", ""))
        self.gate = IntegrationGate(self.store)
        planner = AOOrchestratorPlannerProvider(
            project_id=mission_dict["project_id"], timeout=120)
        auditor = ClaudeCliAuditorProvider(timeout=120)
        verifier = ClaudeCliVerifierProvider(timeout=120)
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


def build_runtime(mission_dict: dict, cfg: dict, *,
                  dry_run: bool = False) -> MissionRuntime:
    return MissionRuntime(mission_dict, cfg, dry_run=dry_run)


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

    mission_dict = json.loads(Path(args.mission_json).read_text("utf-8"))
    cfg = load_config()
    setup_environment()
    rt = build_runtime(mission_dict, cfg, dry_run=args.dry_run)

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
