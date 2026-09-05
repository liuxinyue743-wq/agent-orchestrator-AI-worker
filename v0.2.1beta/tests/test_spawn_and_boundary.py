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
from pathlib import Path
import subprocess
from unittest.mock import MagicMock

from loopcore.action_executor import ActionExecutor
from loopcore.auditor import FakeAuditorProvider
from loopcore.closed_loop import ClosedLoop
from loopcore.event_observer import Observer
from loopcore.mission_contracts import (MissionSpec, PlannerAction,
                                        PlannerActionType, ProjectState,
                                        TaskSpec)
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


def test_worker_contract_defaults_to_codex():
    direct_task = TaskSpec(
        task_id="T-CODEX", project_id="scratch", objective="reply only",
        allowed_paths=[], forbidden_paths=[], acceptance_criteria=[],
        gate_commands=[])
    assert direct_task.worker_harness == "codex"

    task = TaskSpec.from_dict(_task_spec())
    assert task.worker_harness == "codex"

    mission = MissionSpec.from_dict({
        "mission_id": "M-CODEX",
        "project_id": "scratch",
        "objective": "reply only",
        "allowed_paths": [],
        "forbidden_paths": [],
        "acceptance_criteria": [],
        "gate_commands": [],
    })
    assert mission.worker_harness == "codex"


def test_codex_spawn_argv_and_session_id(tmp_path, monkeypatch):
    store = StateStore(str(tmp_path / "cl.db"))
    ex = ActionExecutor("ao", "d", "r", store,
                        worker_model="gpt-5.6-sol")
    task = TaskSpec.from_dict(_task_spec())
    proc = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout="spawned session scratch-1\n", stderr="")
    run = MagicMock(return_value=proc)
    monkeypatch.setattr(ex, "_run", run)

    assert ex._spawn(task.project_id, task.worker_harness,
                     "worker-test", "reply only") == "scratch-1"
    assert run.call_count == 1
    argv = run.call_args.args[0]
    assert argv[:5] == ["spawn", "--kind", "worker", "--project",
                        task.project_id]
    assert argv[argv.index("--harness") + 1] == "codex"
    assert argv[argv.index("--mode") + 1] == "chat"
    assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"


def test_model_rejection_does_not_retry_without_model(tmp_path, monkeypatch):
    store = StateStore(str(tmp_path / "cl.db"))
    ex = ActionExecutor("ao", "d", "r", store,
                        worker_model="gpt-5.6-sol")
    rejected = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="",
        stderr="Invalid value for config option model")
    run = MagicMock(return_value=rejected)
    monkeypatch.setattr(ex, "_run", run)

    assert ex._spawn("scratch", "codex", "worker-test", "reply only") is None
    assert run.call_count == 1
    assert "--model" in run.call_args.args[0]


def test_replan_spawn_keeps_codex_harness_and_model(tmp_path, monkeypatch):
    store = StateStore(str(tmp_path / "cl.db"))
    ex = ActionExecutor("ao", "d", "r", store,
                        worker_model="gpt-5.6-sol")
    task = TaskSpec.from_dict(_task_spec())
    task.worker_session_id = "w-old"
    action = PlannerAction(
        action_id="ACT-CODEX-REPLAN", task_id=task.task_id,
        action=PlannerActionType.REPLAN_SPAWN, reason="retry",
        replacement_task_spec={"objective": "reply only"})
    calls = []

    def fake_run(argv, *args, **kwargs):
        calls.append(argv)
        if argv[:2] == ["session", "kill"]:
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="killed", stderr="")
        return subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="spawned session w-new\n", stderr="")

    monkeypatch.setattr(ex, "_run", fake_run)
    result = ex.execute(action, task)

    assert result.ok and result.new_worker_session_id == "w-new"
    spawn_argv = calls[1]
    assert spawn_argv[spawn_argv.index("--harness") + 1] == "codex"
    assert spawn_argv[spawn_argv.index("--model") + 1] == "gpt-5.6-sol"


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


def test_default_branch_spawn_failure_is_persistent_and_persisted(
        tmp_path, monkeypatch):
    ex, store = _executor(tmp_path)
    task = TaskSpec.from_dict(_task_spec())
    failure = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="",
        stderr="DEFAULT_BRANCH_UNRESOLVED: default branch is unresolved")
    monkeypatch.setattr(ex, "_run", lambda *_args, **_kwargs: failure)

    assert ex.spawn_initial_worker(task) is None

    assert store.counter_get("spawn_attempts:" + task.task_id) == 1
    assert store.counter_get("spawn_transient:" + task.task_id) == 0
    row = store._conn.execute(
        "SELECT alert_id, payload_json FROM alerts").fetchone()
    payload = json.loads(row[1])
    assert row[0].startswith("spawn-failure:%s:persistent:1:"
                             % task.task_id)
    assert payload == {
        "alert_type": "SPAWN_FAILURE",
        "task_id": task.task_id,
        "classification": "persistent",
        "attempt": 1,
        "summary": ("DEFAULT_BRANCH_UNRESOLVED: default branch is "
                    "unresolved"),
    }


def test_spawn_classifier_uses_network_text_after_300_chars(
        tmp_path, monkeypatch):
    ex, store = _executor(tmp_path)
    task = TaskSpec.from_dict(_task_spec())
    failure = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="",
        stderr=("x" * 350) + " upstream connection timed out")
    monkeypatch.setattr(ex, "_run", lambda *_args, **_kwargs: failure)

    assert ex.spawn_initial_worker(task) is None

    assert store.counter_get("spawn_transient:" + task.task_id) == 1
    assert store.counter_get("spawn_attempts:" + task.task_id) == 0


def test_spawn_timeout_never_persists_prompt_argv(tmp_path, monkeypatch):
    ex, store = _executor(tmp_path)
    task_dict = _task_spec()
    task_dict["objective"] = "R5_PRIVATE_OBJECTIVE_SENTINEL"
    task_dict["acceptance_criteria"] = [{
        "id": "AC-PRIVATE",
        "description": "R5_PRIVATE_ACCEPTANCE_SENTINEL",
    }]
    task_dict["gate_commands"] = ["R5_PRIVATE_GATE_SENTINEL"]
    task = TaskSpec.from_dict(task_dict)
    leaked = {}

    def timeout(argv, *_args, **_kwargs):
        error = subprocess.TimeoutExpired(cmd=["ao"] + argv, timeout=120)
        leaked["exception_text"] = str(error)
        raise error

    monkeypatch.setattr(ex, "_run", timeout)

    assert ex.spawn_initial_worker(task) is None
    assert "R5_PRIVATE_OBJECTIVE_SENTINEL" in leaked["exception_text"]
    assert "R5_PRIVATE_ACCEPTANCE_SENTINEL" in leaked["exception_text"]
    assert "R5_PRIVATE_GATE_SENTINEL" in leaked["exception_text"]
    assert store.counter_get("spawn_transient:" + task.task_id) == 1
    assert store.counter_get("spawn_attempts:" + task.task_id) == 0

    payload_json = store._conn.execute(
        "SELECT payload_json FROM alerts").fetchone()[0]
    payload = json.loads(payload_json)
    assert payload["alert_type"] == "SPAWN_FAILURE"
    assert payload["classification"] == "transient"
    assert payload["attempt"] == 1
    assert "timed out" in payload["summary"]
    assert "120" in payload["summary"]

    detail = ex.spawn_budget_detail(task.task_id)
    private_values = (
        "R5_PRIVATE_OBJECTIVE_SENTINEL",
        "R5_PRIVATE_ACCEPTANCE_SENTINEL",
        "R5_PRIVATE_GATE_SENTINEL",
        "Task:",
        "Acceptance criteria:",
        "The gate command for this task:",
        "--prompt",
    )
    for private in private_values:
        assert private not in payload_json
        assert private not in detail


def test_spawn_failure_alert_is_bounded_and_sanitized(tmp_path, monkeypatch):
    ex, store = _executor(tmp_path)
    task = TaskSpec.from_dict(_task_spec())
    home = str(Path.home())
    secret = (
        "DEFAULT_BRANCH_UNRESOLVED Authorization: Bearer bearer-secret "
        "OPENAI_API_KEY=api-secret token=token-secret Cookie=cookie-secret "
        + home + "\\private [request host/request-credential] "
        "--prompt private prompt text --model gpt-5.6-sol " + "z" * 2000)
    failure = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=secret)
    monkeypatch.setattr(ex, "_run", lambda *_args, **_kwargs: failure)

    assert ex.spawn_initial_worker(task) is None

    payload_json = store._conn.execute(
        "SELECT payload_json FROM alerts").fetchone()[0]
    payload = json.loads(payload_json)
    assert payload["alert_type"] == "SPAWN_FAILURE"
    assert payload["classification"] == "persistent"
    assert "DEFAULT_BRANCH_UNRESOLVED" in payload["summary"]
    assert len(payload["summary"]) <= 1600
    for forbidden in (
            "bearer-secret", "api-secret", "token-secret", "cookie-secret",
            home, "request-credential", "private prompt text"):
        assert forbidden not in payload_json


def test_spawn_budget_detail_includes_last_sanitized_root_cause(
        tmp_path, monkeypatch):
    ex, _store = _executor(tmp_path, cap=1)
    task = TaskSpec.from_dict(_task_spec())
    failure = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="",
        stderr="DEFAULT_BRANCH_UNRESOLVED: configure default branch")
    monkeypatch.setattr(ex, "_run", lambda *_args, **_kwargs: failure)

    assert ex.spawn_initial_worker(task) is None

    detail = ex.spawn_budget_detail(task.task_id)
    assert "persistent 1/1, transient 0/8" in detail
    assert "last spawn failure: DEFAULT_BRANCH_UNRESOLVED" in detail


def test_successful_spawn_records_no_failure_alert(tmp_path, monkeypatch):
    ex, store = _executor(tmp_path)
    task = TaskSpec.from_dict(_task_spec())
    success = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="spawned session worker-ok\n",
        stderr="")
    monkeypatch.setattr(ex, "_run", lambda *_args, **_kwargs: success)

    assert ex.spawn_initial_worker(task) == "worker-ok"
    assert store._conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


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
