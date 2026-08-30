"""Regression (review 簇五):
  - initial spawn retries are BOUNDED (cap + linear backoff, counters in the
    store so they survive restart); success clears the counters.
  - a crash-resumed REPLAN_SPAWN idempotent return carries the replacement
    worker session id (previously dropped -> the loop tracked the KILLED
    old worker forever).
  - ClosedLoop.step / MissionController.step never propagate an unexpected
    exception into the runner thread.
  - the AO run-file port parser is not fooled by compact single-line JSON
    (pid must never be read as the port).
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

from loopcore.action_executor import ActionExecutor
from loopcore.auditor import FakeAuditorProvider
from loopcore.closed_loop import ClosedLoop
from loopcore.event_observer import Observer
from loopcore.mission_contracts import (PlannerAction, PlannerActionType,
                                        ProjectState, TaskSpec)
from loopcore.mission_gate import IntegrationGate
from loopcore.planner_adapter import FakePlannerProvider
from loopcore.state_store import StateStore
from loopcore.verifier import FakeVerifierProvider
from tests.sidecar_port.test_phase3 import _cfg
from tests.sidecar_port.test_contracts import _task_spec


def _executor(tmp_path, **kw):
    store = StateStore(str(tmp_path / "cl.db"))
    ex = ActionExecutor("ao", "d", "r", store,
                        max_spawn_attempts=kw.get("cap", 3),
                        spawn_backoff_seconds=kw.get("backoff", 30))
    return ex, store


def _failed_proc(rc=1):
    return subprocess.CompletedProcess(args=[], returncode=rc,
                                       stdout="", stderr="boom")


def test_spawn_retry_cap_and_backoff(tmp_path, monkeypatch):
    ex, store = _executor(tmp_path, cap=3, backoff=30)
    task = TaskSpec.from_dict(_task_spec())
    monkeypatch.setattr(ex, "_run", lambda *a, **k: _failed_proc())
    monkeypatch.setattr("loopcore.action_executor._epoch_seconds",
                        lambda: 1000)
    # attempt 1 -> fail, next allowed at 1030
    assert ex.spawn_initial_worker(task) is None
    assert store.counter_get("spawn_attempts:" + task.task_id) == 1
    # inside the backoff window: no new attempt is counted
    assert ex.spawn_initial_worker(task) is None
    assert store.counter_get("spawn_attempts:" + task.task_id) == 1
    # past backoff: attempts 2 and 3
    monkeypatch.setattr("loopcore.action_executor._epoch_seconds",
                        lambda: 2000)
    assert ex.spawn_initial_worker(task) is None
    monkeypatch.setattr("loopcore.action_executor._epoch_seconds",
                        lambda: 5000)
    assert ex.spawn_initial_worker(task) is None
    assert store.counter_get("spawn_attempts:" + task.task_id) == 3
    # cap reached: never tries again even after the backoff window
    assert ex.spawn_cap_reached(task.task_id) is True
    monkeypatch.setattr("loopcore.action_executor._epoch_seconds",
                        lambda: 99999)
    assert ex.spawn_initial_worker(task) is None
    assert store.counter_get("spawn_attempts:" + task.task_id) == 3


def test_spawn_success_clears_retry_counters(tmp_path, monkeypatch):
    ex, store = _executor(tmp_path)
    task = TaskSpec.from_dict(_task_spec())
    monkeypatch.setattr(ex, "_run", lambda *a, **k: _failed_proc())
    monkeypatch.setattr("loopcore.action_executor._epoch_seconds",
                        lambda: 1000)
    assert ex.spawn_initial_worker(task) is None
    ok_proc = subprocess.CompletedProcess(args=[], returncode=0,
                                          stdout="spawned session s-new\n",
                                          stderr="")
    monkeypatch.setattr(ex, "_run", lambda *a, **k: ok_proc)
    monkeypatch.setattr("loopcore.action_executor._epoch_seconds",
                        lambda: 2000)
    assert ex.spawn_initial_worker(task) == "s-new"
    assert store.counter_get("spawn_attempts:" + task.task_id) == 0
    assert store.counter_get("spawn_next_at:" + task.task_id) == 0


def test_replan_crash_resume_restores_worker_id(tmp_path, monkeypatch):
    ex, store = _executor(tmp_path)
    task = TaskSpec.from_dict(_task_spec())
    task.worker_session_id = "w-old"
    action = PlannerAction(action_id="ACT-REPLAN-1", task_id=task.task_id,
                           action=PlannerActionType.REPLAN_SPAWN,
                           replacement_task_spec={"objective": "redo"},
                           reason="t")
    ok_proc = subprocess.CompletedProcess(args=[], returncode=0,
                                          stdout="spawned session w-new\n",
                                          stderr="")
    monkeypatch.setattr(ex, "_run", lambda *a, **k: ok_proc)
    first = ex.execute(action, task)
    assert first.ok and first.new_worker_session_id == "w-new"
    # crash-resume: same action_id executed again -> idempotent return MUST
    # carry the replacement worker id from the stored result.
    monkeypatch.setattr(ex, "_run", lambda *a, **k: _failed_proc())
    second = ex.execute(action, task)
    assert second.ok and second.new_worker_session_id == "w-new"
    assert "idempotent" in second.detail


def _make_loop(tmp_path, monkeypatch):
    store = StateStore(str(tmp_path / "cl.db"))
    task = TaskSpec.from_dict(_task_spec())
    task.worker_session_id = "w-step"
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path))
    adapter = MagicMock()
    adapter.get_recent_events.return_value = []
    loop = ClosedLoop(task=task, cfg=_cfg(), auditor=FakeAuditorProvider(),
                      planner=FakePlannerProvider(),
                      executor=ActionExecutor("ao", "d", "r", store),
                      observer=Observer(_cfg(), state_store=store),
                      adapter=adapter, gate=IntegrationGate(store),
                      store=store, verifier=FakeVerifierProvider())
    return loop, store


def test_step_never_raises(tmp_path, monkeypatch):
    loop, store = _make_loop(tmp_path, monkeypatch)
    def boom(*a, **k):
        raise TypeError("'NoneType' object is not iterable")
    monkeypatch.setattr(loop, "_runtime_exceeded", boom)
    result = loop.step()            # must NOT raise
    assert result["acted"] is False
    assert "NoneType" in result["error"]
    rows = store._conn.execute(
        "SELECT payload_json FROM alerts").fetchall()
    assert any("LOOP_ERROR" in r[0] for r in rows)


def test_run_file_port_parsing_compact_json(tmp_path, monkeypatch):
    from loopcore.ao_adapter import _port_from_run_file
    rf = tmp_path / "ao.run"
    rf.write_text('{"pid": 25612, "port": 4567}', encoding="utf-8")
    monkeypatch.setenv("AO_RUN_FILE", str(rf))
    assert _port_from_run_file() == "4567"   # NOT the pid
