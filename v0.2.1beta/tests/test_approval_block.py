"""Regression: a worker blocked on a pending permission prompt must NOT be
audited as 'idle -> completion'.

Real-run evidence (MISSION-QUICK-008 S2): the claude-code worker issued two
Write calls, the harness raised an approval request, the worker then reported
waiting_input. The loop read that as 'worker finished', ran three completion
audits against an untouched worktree, burned both LOCAL_FIX rounds, and
escalated to HUMAN — while the fix approval sat pending and resolvable.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from loopcore.action_executor import ActionExecutor
from loopcore.auditor import FakeAuditorProvider
from loopcore.closed_loop import ClosedLoop
from loopcore.event_observer import Observer
from loopcore.mission_contracts import ProjectState, TaskSpec
from loopcore.mission_gate import IntegrationGate
from loopcore.planner_adapter import FakePlannerProvider
from loopcore.state_store import StateStore
from loopcore.verifier import FakeVerifierProvider
from tests.sidecar_port.test_phase3 import _cfg
from tests.sidecar_port.test_contracts import _task_spec
from tests.sidecar_port.util import ev


def _pending_approval(worktree: str, rel: str, req_id: str) -> dict:
    return {
        "kind": "activity", "activityKind": "approval", "status": "pending",
        "providerItemId": req_id, "id": "act-" + req_id,
        "summary": "Write " + rel,
        "detail": {"subjectKind": "file_change", "toolKind": "edit",
                   "input": {"file_path": worktree + "/" + rel,
                             "content": "def cube(a):\n    return a*a*a\n"},
                   "decisions": [{"id": "allow", "kind": "allow_once"}]},
    }


def _make_loop(tmp_path, monkeypatch, state, pending):
    store = StateStore(str(tmp_path / "cl.db"))
    task = TaskSpec.from_dict(_task_spec())
    task.worker_session_id = "w-blocked"
    task.allowed_paths = ["math2.py", "tests/test_cube.py"]
    wt = tmp_path / "worktrees" / task.project_id / task.worker_session_id
    wt.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(wt), check=True)
    adapter = MagicMock()
    adapter.get_session_workspace.return_value = str(wt)
    adapter.get_recent_events.return_value = []
    adapter.get_worker_status.return_value = {"id": task.worker_session_id,
                                              "status": "waiting_input"}
    adapter.get_worker_conversation.return_value = {
        "activities": [_pending_approval(str(wt), "math2.py", "req-1")]
        if pending else []}
    adapter.resolve_approval.return_value = True
    ex = ActionExecutor("ao", "d", "r", store)
    loop = ClosedLoop(task=task, cfg=_cfg(), auditor=FakeAuditorProvider(),
                      planner=FakePlannerProvider(), executor=ex,
                      observer=Observer(_cfg(), state_store=store),
                      adapter=adapter, gate=IntegrationGate(store),
                      store=store, verifier=FakeVerifierProvider())
    for to in {ProjectState.WORKER_RETRYING:
               [ProjectState.WORKER_RUNNING, ProjectState.AUDIT_PENDING,
                ProjectState.PLANNER_PENDING, ProjectState.LOCAL_FIX_PENDING,
                ProjectState.WORKER_RETRYING],
               ProjectState.WORKER_RUNNING: [ProjectState.WORKER_RUNNING],
               }[state]:
        loop._transition(to, "test", "setup", {})
    return loop, store, adapter


def test_retrying_worker_blocked_on_approval_is_not_audited(tmp_path,
                                                            monkeypatch):
    loop, store, adapter = _make_loop(tmp_path, monkeypatch,
                                      ProjectState.WORKER_RETRYING, True)
    result = loop.step()
    assert result["acted"] is True
    # approval resolved, worker left alone to continue, NO audit happened
    adapter.resolve_approval.assert_called_once_with("w-blocked", "req-1",
                                                     "allow")
    assert loop.state == ProjectState.WORKER_RETRYING
    assert store._conn.execute("SELECT COUNT(*) FROM audits").fetchone()[0] == 0


def test_running_worker_blocked_on_approval_is_not_idle(tmp_path, monkeypatch):
    loop, store, adapter = _make_loop(tmp_path, monkeypatch,
                                      ProjectState.WORKER_RUNNING, True)
    result = loop.step(injected_events=[ev("2026-08-30T00:00:00Z",
                                           worker="w-blocked",
                                           etype="message")])
    # _maybe_idle_completion must defer; auto-approval fires instead
    adapter.resolve_approval.assert_called_once_with("w-blocked", "req-1",
                                                     "allow")
    assert loop.state == ProjectState.WORKER_RUNNING
    assert store._conn.execute("SELECT COUNT(*) FROM audits").fetchone()[0] == 0


def test_gate_command_with_cd_prefix(tmp_path, monkeypatch):
    """`cd "<worktree>" && python -m pytest ...` must be recognized as a
    safe pytest invocation; cd elsewhere must not."""
    loop, store, adapter = _make_loop(tmp_path, monkeypatch,
                                      ProjectState.WORKER_RUNNING, False)
    wt = loop._worktree_path()
    assert loop._is_gate_command(
        'cd "%s" && python -m pytest tests/ -v' % wt) is True
    assert loop._is_gate_command(
        "cd '%s' && python -m pytest -q" % wt) is True
    assert loop._is_gate_command(
        'cd "%s" && git status' % wt) is True
    assert loop._is_gate_command(
        'cd "%s" && ls -la && command -v python' % wt) is True
    assert loop._is_gate_command(
        'cd "%s" && python -m pytest' % "C:/Windows/System32") is False
    assert loop._is_gate_command(
        'cd "%s" && del /f app.py' % wt) is False
    assert loop._is_gate_command(
        'cd "%s" && ls && rm -rf tests' % wt) is False
    assert loop._is_gate_command(
        'cd "%s" && echo hacked > app.py' % wt) is False
    # plain commands keep working
    assert loop._is_gate_command("python -m pytest tests/ -q") is True


def test_subshell_parens_unwrapped(tmp_path, monkeypatch):
    loop, store, adapter = _make_loop(tmp_path, monkeypatch,
                                      ProjectState.WORKER_RUNNING, False)
    wt = loop._worktree_path()
    assert loop._is_gate_command(
        'cd "%s" && ls -la && (command -v python)' % wt) is True
    assert loop._is_gate_command('(command -v pytest)') is True
    assert loop._is_gate_command('(rm -rf tests)') is False


def test_ready_state_blocked_worker_is_approved(tmp_path, monkeypatch):
    """簇八 (MISSION-QUICK-014 S1): a worker whose FIRST turn hits a
    permission prompt never emits activity, so the loop is still TASK_READY
    when the blocked-pause branch runs. The auto-approve state gate must
    include TASK_READY or the resolvable approval sits until HUMAN."""
    loop, store, adapter = _make_loop(tmp_path, monkeypatch,
                                      ProjectState.WORKER_RUNNING, True)
    # state reads from the STORE (latest transition); remove the setup
    # transitions so the loop is genuinely back in TASK_READY.
    store._conn.execute("DELETE FROM state_transitions")
    store._conn.commit()
    assert loop.state == ProjectState.TASK_READY
    result = loop.step()
    adapter.resolve_approval.assert_called_once_with("w-blocked", "req-1",
                                                     "allow")
    assert result["acted"] is True
