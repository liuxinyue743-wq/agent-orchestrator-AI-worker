"""Closed-loop CLI.

  python -m src.closed_loop_cli --task tasks/demo-repeated-error.json --once
  python -m src.closed_loop_cli --task tasks/demo-repeated-error.json --watch
  python -m src.closed_loop_cli --task tasks/demo-repeated-error.json --dry-run

Logs: runtime/{events,alerts,audits,planner_actions,state_transitions,gate_runs}.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .action_executor import ActionExecutor
from .ao_adapter import AOAdapter
from .auditor import ClaudeCliAuditorProvider, FakeAuditorProvider
from .closed_loop import ClosedLoop, RUNTIME
from .contracts import TaskSpec
from .integration_gate import IntegrationGate
from .observer import Observer
from .planner_adapter import (AOOrchestratorPlannerProvider, FakePlannerProvider)
from .state_store import StateStore
from .verifier import (ClaudeCliVerifierProvider, FakeVerifierProvider,
                       VerifierProvider)

ROOT = Path(__file__).resolve().parent.parent
_JSONL_LOCK = threading.Lock()


def _jsonl(name: str) -> Path:
    p = RUNTIME / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _append(name: str, obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with _JSONL_LOCK:
        with open(_jsonl(name), "a", encoding="utf-8") as f:
            f.write(line)


# Patch StateStore methods to also mirror to JSONL for auditability.
def _wire_jsonl(store: StateStore) -> None:
    _orig_audit = store.record_audit
    _orig_action = store.record_action
    _orig_trans = store.record_transition
    _orig_gate = store.record_gate_run
    _orig_verif = store.record_verification

    def record_audit(audit_id, task_id, payload):
        _orig_audit(audit_id, task_id, payload)
        _append("audits.jsonl", payload)
    def record_action(action_id, task_id, payload):
        _orig_action(action_id, task_id, payload)
        _append("planner_actions.jsonl", payload)
    def record_transition(**kw):
        _orig_trans(**kw)
        _append("state_transitions.jsonl", kw)
    def record_gate_run(**kw):
        _orig_gate(**kw)
        _append("gate_runs.jsonl", kw)
    def record_verification(verify_id, task_id, payload):
        _orig_verif(verify_id, task_id, payload)
        _append("verifications.jsonl", payload)
    store.record_audit = record_audit  # type: ignore
    store.record_action = record_action  # type: ignore
    store.record_transition = record_transition  # type: ignore
    store.record_gate_run = record_gate_run  # type: ignore
    store.record_verification = record_verification  # type: ignore
    store.record_gate_run = record_gate_run  # type: ignore


def build_loop(cfg: dict, task: TaskSpec, *, dry_run: bool,
               fake: bool = False, instruct: str = "",
               db_name: str = "closed_loop.db") -> ClosedLoop:
    import os
    store = StateStore(ROOT / "runtime" / db_name)
    _wire_jsonl(store)
    store.record_task(task.task_id, task.to_dict())
    adapter = AOAdapter(cfg["ao"]["base_url"],
                       timeout=float(cfg["ao"].get("request_timeout_seconds", 15)))
    if fake:
        auditor = FakeAuditorProvider()
        planner = FakePlannerProvider()
        verifier = FakeVerifierProvider()
    else:
        auditor = ClaudeCliAuditorProvider()
        planner = AOOrchestratorPlannerProvider(
            ao_bin=str(ROOT.parent / "ao-app" / "resources" / "daemon" / "ao.exe"),
            project_id=task.project_id,
            data_dir=os.environ["AO_DATA_DIR"],
            run_file=os.environ["AO_RUN_FILE"])
        verifier = ClaudeCliVerifierProvider()
    executor = ActionExecutor(
        ao_bin=str(ROOT.parent / "ao-app" / "resources" / "daemon" / "ao.exe"),
        data_dir=os.environ.get("AO_DATA_DIR", ""),
        run_file=os.environ.get("AO_RUN_FILE", ""),
        store=store,
        worker_model=cfg.get("worker", {}).get("model", ""))
    gate = IntegrationGate(store)
    return ClosedLoop(task=task, cfg=cfg, auditor=auditor, planner=planner,
                     executor=executor, observer=observer, adapter=adapter,
                     gate=gate, store=store, verifier=verifier,
                     dry_run=dry_run,
                     instruct=instruct)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="closed-loop")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true")
    g.add_argument("--watch", action="store_true")
    g.add_argument("--dry-run", action="store_true")
    ap.add_argument("--task", required=True, help="path to TaskSpec json")
    ap.add_argument("--config", default=None)
    ap.add_argument("--worker-session", default=None,
                    help="override task.worker_session_id (auto-bound by demo script)")
    ap.add_argument("--instruct", default="",
                    help="top-level user directive the Planner absorbs and folds "
                         "into its strategy every cycle (leader capability)")
    ap.add_argument("--db", default=None,
                    help="state-store filename under runtime/ (default "
                         "closed_loop.db); a fresh name gives a clean run "
                         "without re-processing already-seen events")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from .cli import load_config
    cfg = load_config(args.config)
    with open(args.task, encoding="utf-8") as f:
        task = TaskSpec.from_dict(json.load(f))
    if args.worker_session:
        task.worker_session_id = args.worker_session

    dry = args.dry_run
    loop = build_loop(cfg, task, dry_run=dry, fake=dry,
                      instruct=args.instruct,
                      db_name=args.db or "closed_loop.db")

    if args.once or args.dry_run:
        r = loop.step()
        print("state=%s acted=%s" % (r["state"], r["acted"]))
        return 0
    # --watch
    poll = float(cfg["ao"].get("poll_interval_seconds", 10))
    print("closed-loop watching (poll %.0fs, Ctrl+C to stop)" % poll)
    try:
        while True:
            r = loop.step()
            if r["acted"]:
                print("state=%s acted=True" % r["state"])
            if r["state"] in ("DONE", "HUMAN", "FAILED"):
                print("terminal state: %s" % r["state"])
                break
            time.sleep(poll)
    except KeyboardInterrupt:
        print("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
