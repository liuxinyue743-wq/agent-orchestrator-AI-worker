"""Mission CLI: one user instruction -> fully automatic multi-worker run.

  python -m src.mission_cli --mission tasks/mission-demo.json --watch [--db X]

The user gives ONE complete instruction (MissionSpec json). The leader
Planner decomposes it, N workers run in parallel (each with its own closed
loop: observe -> audit -> plan -> act -> gate -> verify), finished subtasks
merge into an integration worktree, and a final gate + mission-level
Verifier decide MISSION_DONE. The only human touchpoint is HUMAN.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .action_executor import ActionExecutor
from .ao_adapter import AOAdapter
from .auditor import ClaudeCliAuditorProvider, FakeAuditorProvider
from .cli import load_config
from .mission_contracts import MissionSpec
from .mission_gate import IntegrationGate
from .mission import MissionController
from .event_observer import Observer
from .planner_adapter import (AOOrchestratorPlannerProvider,
                              FakePlannerProvider)
from .state_store import StateStore
from .verifier import ClaudeCliVerifierProvider, FakeVerifierProvider

ROOT = Path(__file__).resolve().parent.parent


def build_controller(cfg: dict, mission: MissionSpec, *, dry_run: bool,
                     db_name: str = "closed_loop.db") -> MissionController:
    import os
    store = StateStore(ROOT / "runtime" / db_name)
    # reuse the single-task CLI's jsonl mirroring (audits/actions/...)
    from .closed_loop_cli import _wire_jsonl
    _wire_jsonl(store)
    adapter = AOAdapter(cfg["ao"]["base_url"],
                        timeout=float(cfg["ao"].get(
                            "request_timeout_seconds", 15)))
    if dry_run:
        planner = FakePlannerProvider()
        auditor = FakeAuditorProvider()
        verifier = FakeVerifierProvider()
    else:
        planner = AOOrchestratorPlannerProvider(
            ao_bin=str(ROOT.parent / "ao-app" / "resources" / "daemon" /
                       "ao.exe"),
            project_id=mission.project_id,
            data_dir=os.environ["AO_DATA_DIR"],
            run_file=os.environ["AO_RUN_FILE"])
        auditor = ClaudeCliAuditorProvider()
        verifier = ClaudeCliVerifierProvider()
    executor = ActionExecutor(
        ao_bin=str(ROOT.parent / "ao-app" / "resources" / "daemon" / "ao.exe"),
        data_dir=os.environ.get("AO_DATA_DIR", ""),
        run_file=os.environ.get("AO_RUN_FILE", ""),
        store=store,
        worker_model=cfg.get("worker", {}).get("model", ""))
    gate = IntegrationGate(store)
    return MissionController(mission=mission, cfg=cfg, planner=planner,
                             auditor=auditor, verifier=verifier,
                             executor=executor, adapter=adapter, gate=gate,
                             store=store, dry_run=dry_run)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mission")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true")
    g.add_argument("--watch", action="store_true")
    g.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mission", required=True, help="path to MissionSpec json")
    ap.add_argument("--config", default=None)
    ap.add_argument("--db", default=None,
                    help="state-store filename under runtime/")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    cfg = load_config(args.config)
    with open(args.mission, encoding="utf-8") as f:
        mission = MissionSpec.from_dict(json.load(f))
    dry = args.dry_run
    mc = build_controller(cfg, mission, dry_run=dry,
                          db_name=args.db or "closed_loop.db")

    if args.once or args.dry_run:
        r = mc.step()
        print("mission state=%s acted=%s" % (r["state"], r["acted"]))
        return 0
    poll = float(cfg["ao"].get("poll_interval_seconds", 10))
    print("mission watching (poll %.0fs, Ctrl+C to stop)" % poll)
    try:
        while True:
            r = mc.step()
            if r["acted"]:
                print("mission state=%s acted=True" % r["state"])
            if r["state"] in ("MISSION_DONE", "HUMAN", "FAILED"):
                print("terminal state: %s" % r["state"])
                break
            time.sleep(poll)
    except KeyboardInterrupt:
        print("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
