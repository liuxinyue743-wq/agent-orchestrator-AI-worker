"""Crash-resume regression: transient PENDING states must re-enter their
handler instead of parking forever.

A process killed mid-chain (tool timeout / Ctrl-C) between two hops of the
synchronous audit -> planner -> execute -> gate -> verifier chain leaves the
loop in a pending state that no poll branch used to pick up (real-run
evidence: MISSION-QUICK-006 S2 sat in PLANNER_PENDING for 4+ minutes after a
mid-planner kill). Each test parks the loop in one such state with only the
store rows a real crash would have left, then asserts one step() recovers.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from loopcore.action_executor import ActionExecutor
from loopcore.auditor import FakeAuditorProvider
from loopcore.closed_loop import ClosedLoop
from loopcore.event_observer import Observer
from loopcore.mission_contracts import (AuditResult, PlannerAction,
                                        ProjectState, TaskSpec)
from loopcore.mission_gate import IntegrationGate
from loopcore.planner_adapter import FakePlannerProvider
from loopcore.state_store import StateStore
from loopcore.verifier import FakeVerifierProvider
from tests.sidecar_port.test_phase3 import _cfg
from tests.sidecar_port.test_contracts import _task_spec


def _make_loop(tmp_path, monkeypatch, states):
    """A ClosedLoop driven through `states` with a real temp git worktree."""
    store = StateStore(str(tmp_path / "cl.db"))
    task = TaskSpec.from_dict(_task_spec())
    task.worker_session_id = "w-crash"
    task.gate_commands = ["python -c \"print('gate ok')\""]
    # worktree layout: <AO_DATA_DIR>/worktrees/<project>/<session>
    wt = tmp_path / "worktrees" / task.project_id / task.worker_session_id
    wt.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(wt), check=True)
    (wt / "app.py").write_text("def divide(a, b):\n    return a / b\n",
                               encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(wt), check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=str(wt), check=True)
    # an uncommitted change after the frozen base: the verifier must see a
    # non-empty diff, or the fake provider FAILs on "nothing to verify".
    (wt / "app.py").write_text("def divide(a, b):\n    return a / b\n"
                               "def square(a):\n    return a * a\n",
                               encoding="utf-8")
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path))
    obs = Observer(_cfg(), state_store=store)
    adapter = MagicMock()
    adapter.get_recent_events.return_value = []
    ex = ActionExecutor("ao", "d", "r", store)
    loop = ClosedLoop(task=task, cfg=_cfg(), auditor=FakeAuditorProvider(),
                      planner=FakePlannerProvider(), executor=ex, observer=obs,
                      adapter=adapter, gate=IntegrationGate(store),
                      store=store, verifier=FakeVerifierProvider())
    # drive the state machine to the target state through the legal path
    for to in states:
        loop._transition(to, "test", "setup", {})
    return loop, store


def _parked_loop(tmp_path, monkeypatch, target):
    """Drive TASK_READY -> ... -> target along a legal route."""
    route = {
        ProjectState.VERIFIER_PENDING: [
            ProjectState.WORKER_RUNNING, ProjectState.AUDIT_PENDING,
            ProjectState.PLANNER_PENDING, ProjectState.GATE_PENDING,
            ProjectState.VERIFIER_PENDING],
        ProjectState.GATE_PENDING: [
            ProjectState.WORKER_RUNNING, ProjectState.AUDIT_PENDING,
            ProjectState.PLANNER_PENDING, ProjectState.GATE_PENDING],
        ProjectState.PLANNER_PENDING: [
            ProjectState.WORKER_RUNNING, ProjectState.AUDIT_PENDING,
            ProjectState.PLANNER_PENDING],
        ProjectState.AUDIT_PENDING: [
            ProjectState.WORKER_RUNNING, ProjectState.AUDIT_PENDING],
        ProjectState.LOCAL_FIX_PENDING: [
            ProjectState.WORKER_RUNNING, ProjectState.AUDIT_PENDING,
            ProjectState.PLANNER_PENDING, ProjectState.LOCAL_FIX_PENDING],
    }[target]
    return _make_loop(tmp_path, monkeypatch, route)


def _pass_audit(task_id):
    return AuditResult(audit_id="AUDIT-CRASH-1", task_id=task_id,
                       decision="PASS", evidence=[], diagnosis="ok",
                       confidence=0.9).to_dict()


def test_verifier_pending_reenters_verifier(tmp_path, monkeypatch):
    """Simulates a crash after 'gate pass' but before the verifier answered."""
    loop, store = _parked_loop(tmp_path, monkeypatch,
                               ProjectState.VERIFIER_PENDING)
    assert loop.state == ProjectState.VERIFIER_PENDING
    assert not store.verification_seen("any")  # nothing recorded yet
    result = loop.step()
    assert result["acted"] is True
    assert loop.state == ProjectState.DONE  # FakeVerifierProvider passes
    verifications = store._conn.execute(
        "SELECT verify_id FROM verifications").fetchall()
    assert len(verifications) == 1


def test_audit_pending_reenters_completion_audit(tmp_path, monkeypatch):
    """Crash mid-audit: only the AUDIT_PENDING transition survived."""
    loop, store = _parked_loop(tmp_path, monkeypatch,
                               ProjectState.AUDIT_PENDING)
    assert loop.state == ProjectState.AUDIT_PENDING
    result = loop.step()
    assert result["acted"] is True
    # FakeAuditor sees a green gate -> PASS -> planner CANDIDATE_DONE ->
    # gate -> verifier: the whole chain completes in one resumed step.
    assert loop.state == ProjectState.DONE
    audits = store._conn.execute("SELECT audit_id FROM audits").fetchall()
    assert len(audits) == 1


def test_planner_pending_resumes_planner(tmp_path, monkeypatch):
    """Crash between the PLANNER_PENDING transition and the planner call:
    the audit row survived, no planner action exists yet."""
    loop, store = _parked_loop(tmp_path, monkeypatch,
                               ProjectState.PLANNER_PENDING)
    store.record_audit("AUDIT-CRASH-1", loop.task.task_id,
                       _pass_audit(loop.task.task_id))
    assert not store.action_seen("ACTION-CRASH-1")
    result = loop.step()
    assert result["acted"] is True
    # PASS audit -> CANDIDATE_DONE -> gate -> verifier -> DONE
    assert loop.state == ProjectState.DONE
    actions = store._conn.execute(
        "SELECT action_id FROM planner_actions").fetchall()
    assert len(actions) == 1


def test_local_fix_pending_resumes_action_idempotent(tmp_path, monkeypatch):
    """Crash after the fix was SENT but before the WORKER_RETRYING
    transition: resume must advance the machine WITHOUT re-sending."""
    loop, store = _parked_loop(tmp_path, monkeypatch,
                               ProjectState.LOCAL_FIX_PENDING)
    pa = PlannerAction(action_id="ACTION-CRASH-2", task_id=loop.task.task_id,
                       action="SEND_LOCAL_FIX", reason="fix",
                       target_session_id="w-crash", message="please fix")
    store.record_action(pa.action_id, pa.task_id, pa.to_dict())
    # the original run already sent the message successfully
    store.mark_action_executed(pa.action_id, {"ok": True, "detail": "sent"})
    result = loop.step()
    assert result["acted"] is True
    assert loop.state == ProjectState.WORKER_RETRYING
    # exactly one executed action row: no double side effect
    rows = store._conn.execute(
        "SELECT action_id FROM executed_actions").fetchall()
    assert rows == [("ACTION-CRASH-2",)]


def test_gate_pending_resumes_gate(tmp_path, monkeypatch):
    """Crash after CANDIDATE_DONE but before the gate ran."""
    loop, store = _parked_loop(tmp_path, monkeypatch,
                               ProjectState.GATE_PENDING)
    result = loop.step()
    assert result["acted"] is True
    assert loop.state == ProjectState.DONE  # gate pass -> verifier PASS
    runs = store._conn.execute(
        "SELECT command FROM gate_runs WHERE command != 'path-gate'"
    ).fetchall()
    assert len(runs) == 1
