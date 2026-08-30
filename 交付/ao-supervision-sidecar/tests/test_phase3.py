"""Phase 3 tests: integration gate + closed-loop flow (no real AO)."""
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.action_executor import ActionExecutor
from src.auditor import FakeAuditorProvider, EvidenceBundle
from src.closed_loop import ClosedLoop
from src.contracts import (AuditDecision, AuditResult, AuditEvidence,
                           PlannerAction, PlannerActionType, ProjectState,
                           TaskSpec)
from src.integration_gate import IntegrationGate
from src.observer import Observer
from src.planner_adapter import FakePlannerProvider
from src.state_store import StateStore
from tests.test_contracts import _task_spec
from tests.util import ev


def _cfg():
    return {
        "ao": {"base_url": "http://127.0.0.1:1", "request_timeout_seconds": 1,
               "sse_idle_timeout_seconds": 1, "poll_interval_seconds": 1},
        "thresholds": {
            "repeated_error": {"window_seconds": 600, "count": 3,
                               "cooldown_seconds": 600},
            "no_progress": {"window_seconds": 900, "min_activity_events": 8,
                            "max_progress_events": 0, "cooldown_seconds": 900,
                            "progress_mode": "strong"},
        },
    }


def _loop(tmp_path, *, dry=False):
    store = StateStore(tmp_path / "cl.db")
    task = TaskSpec.from_dict(_task_spec())
    task.worker_session_id = "w1"
    obs = Observer(_cfg(), state_store=store)
    adapter = MagicMock()
    adapter.get_recent_events.return_value = []
    ex = ActionExecutor("ao", "d", "r", store)
    gate = IntegrationGate(store)
    auditor = FakeAuditorProvider()
    planner = FakePlannerProvider()
    return ClosedLoop(task=task, cfg=_cfg(), auditor=auditor, planner=planner,
                     executor=ex, observer=obs, adapter=adapter, gate=gate,
                     store=store, dry_run=dry), task, store


def test_gate_pass(tmp_path):
    store = StateStore(tmp_path / "cl.db")
    gate = IntegrationGate(store)
    # write app.py with working divide
    d = tmp_path / "repo"
    d.mkdir()
    (d / "app.py").write_text(
        "def add(a,b):\n return a+b\n"
        "def divide(a,b):\n"
        "  if b==0:\n    raise ValueError\n"
        "  return a/b\n", encoding="utf-8")
    (d / "tests").mkdir()
    (d / "tests" / "test_divide.py").write_text(
        "def test_ok():\n from app import divide\n assert divide(6,3)==2\n"
        "def test_zero():\n from app import divide\n"
        " import pytest\n"
        " with pytest.raises(ValueError):\n  divide(1,0)\n", encoding="utf-8")
    task = TaskSpec.from_dict(_task_spec())
    run = gate.run(task, str(d))
    assert run.ok
    assert all(r["exit_code"] == 0 for r in run.results)


def test_gate_failure(tmp_path):
    store = StateStore(tmp_path / "cl.db")
    gate = IntegrationGate(store)
    d = tmp_path / "repo"
    d.mkdir()
    (d / "app.py").write_text("def add(a,b):\n return a+b\n", encoding="utf-8")
    (d / "tests").mkdir()
    (d / "tests" / "test_divide.py").write_text(
        "def test_ok():\n from app import divide\n assert divide(6,3)==2\n",
        encoding="utf-8")
    task = TaskSpec.from_dict(_task_spec())
    run = gate.run(task, str(d))
    assert not run.ok


def test_done_requires_gate(tmp_path):
    loop, task, store = _loop(tmp_path)
    # CANDIDATE_DONE without gate -> cannot jump to DONE; gate must run.
    # In dry-run, _execute returns without gating; state stays.
    # Force gate path manually:
    loop._run_gate = MagicMock()
    # mark candidate done:
    store.record_transition(task_id=task.task_id, from_state=ProjectState.TASK_READY,
        to_state=ProjectState.GATE_PENDING, actor="t", reason="t", evidence={})
    # gate not run -> state is GATE_PENDING, not DONE
    assert loop.state == ProjectState.GATE_PENDING
    assert loop.state != ProjectState.DONE


def test_closed_loop_dry_run_no_actions(tmp_path):
    loop, task, store = _loop(tmp_path, dry=True)
    # feed a fake alert via observer by injecting events
    from tests.util import ev
    # monkeypatch _collect_events to return a 3x repeated error
    errs = [ev("2026-08-27T00:0%d:00Z" % i, etype="error",
               message="conn refused to /x", fingerprint="fp") for i in range(3)]
    loop._collect_events = MagicMock(return_value=errs)
    loop.step()
    # dry-run: no actions executed, but audit + planner action recorded
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "cl.db"))
    audits = conn.execute("SELECT count(*) FROM audits").fetchone()[0]
    actions = conn.execute("SELECT count(*) FROM planner_actions").fetchone()[0]
    executed = conn.execute("SELECT count(*) FROM executed_actions").fetchone()[0]
    conn.close()
    assert audits >= 1
    assert actions >= 1
    assert executed == 0  # dry-run never executes


def test_alert_triggers_one_audit(tmp_path):
    loop, task, store = _loop(tmp_path, dry=True)
    errs = [ev("2026-08-27T00:0%d:00Z" % i, etype="error",
               message="conn refused", fingerprint="fp") for i in range(3)]
    loop._collect_events = MagicMock(return_value=errs)
    loop.step()
    # second step with same events -> no new audit (dedup)
    n_before = _count(store, "audits")
    loop.step()
    n_after = _count(store, "audits")
    assert n_after == n_before


def _count(store, table):
    import sqlite3
    conn = sqlite3.connect(store.path)
    c = conn.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
    conn.close()
    return c
