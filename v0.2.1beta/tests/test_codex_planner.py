"""Offline Planner migration and planning dry-run tests."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import run_mission
from loopcore.auditor import CodexCliAuditorProvider
from loopcore.codex_cli import CodexCliError
from loopcore.mission_contracts import (
    AuditDecision,
    AuditEvidence,
    AuditResult,
    MissionPlan,
    PlannerActionType,
)
from loopcore.planner_adapter import CodexCliPlannerProvider
from loopcore.verifier import CodexCliVerifierProvider


MISSION = {
    "mission_id": "M-CODEX",
    "project_id": "demo",
    "objective": "Implement the requested behavior",
    "allowed_paths": ["app.py"],
    "forbidden_paths": ["tests/**"],
    "acceptance_criteria": [{"id": "AC-1", "description": "works"}],
    "gate_commands": ["python -m pytest -q"],
    "budgets": {"max_subtasks": 1, "max_runtime_seconds": 60},
}


def _audit():
    return AuditResult(
        "AUD-1", "TASK-1", AuditDecision.PASS,
        [AuditEvidence("gate", "all checks passed")], "done", 0.99)


def _replan_audit():
    return AuditResult(
        "AUD-REPLAN", "TASK-1", AuditDecision.REPLAN,
        [AuditEvidence("failure", "the original route cannot succeed")],
        "choose a corrected implementation route", 0.95,
        failed_criteria=["AC-1"])


def _action():
    return {
        "action_id": "ACT-1",
        "task_id": "TASK-1",
        "action": "CANDIDATE_DONE",
        "reason": "audit passed",
        "message": "",
        "plan": "send to gate",
    }


def _replan_action(replacement):
    return {
        "action_id": "ACT-REPLAN",
        "task_id": "TASK-1",
        "action": "REPLAN_SPAWN",
        "target_session_id": "worker-old",
        "message": "",
        "replacement_task_spec": replacement,
        "reason": "the original route is blocked",
        "plan": "spawn a replacement worker with the corrected route",
    }


def _plan():
    return {
        "mission_id": "M-CODEX",
        "strategy": "one bounded worker lane",
        "subtasks": [{
            "subtask_id": "M-CODEX-S1",
            "objective": "Implement the requested behavior",
            "allowed_paths": ["app.py"],
            "acceptance_criteria": [
                {"id": "AC-1", "description": "works"}],
            "dependencies": [],
            "gate_commands": ["python -m pytest -q"],
        }],
    }


def test_plan_returns_valid_action_and_passes_runner_inputs(monkeypatch,
                                                            tmp_path):
    from loopcore import planner_adapter
    calls = []

    def fake_runner(**kwargs):
        calls.append(kwargs)
        return _action()

    monkeypatch.setattr(planner_adapter, "run_codex_json", fake_runner)
    planner = CodexCliPlannerProvider(
        model="model-p", timeout=23, codex_bin="codex-p", cwd=tmp_path)
    action = planner.plan(_audit(), {"task_id": "TASK-1"}, "ACT-1")

    assert action.action == PlannerActionType.CANDIDATE_DONE
    assert action.validate()[0]
    assert len(calls) == 1
    call = calls[0]
    assert call["model"] == "model-p"
    assert call["timeout"] == 23
    assert call["codex_bin"] == "codex-p"
    assert call["cwd"] == tmp_path
    assert Path(call["schema_path"]).name == "planner-action.schema.json"
    assert planner.system_prompt in call["prompt"]
    assert '"task_id": "TASK-1"' in call["prompt"]


def test_plan_decompose_returns_valid_plan_and_uses_schema(monkeypatch,
                                                           tmp_path):
    from loopcore import planner_adapter
    calls = []
    monkeypatch.setattr(
        planner_adapter, "run_codex_json",
        lambda **kwargs: calls.append(kwargs) or _plan())
    planner = CodexCliPlannerProvider(cwd=tmp_path)

    plan = planner.plan_decompose(MISSION, "DECOMP-M-CODEX")

    assert plan.mission_id == "M-CODEX"
    assert len(plan.subtasks) == 1
    assert Path(calls[0]["schema_path"]).name == "mission-plan.schema.json"
    assert planner.decompose_prompt in calls[0]["prompt"]
    assert '"mission_id": "M-CODEX"' in calls[0]["prompt"]
    assert "EXACTLY 1" in calls[0]["prompt"]


def test_plan_retries_once_then_succeeds(monkeypatch):
    planner = CodexCliPlannerProvider()
    calls = iter([CodexCliError("first"), _action()])

    def fake_call(*args, **kwargs):
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(planner, "_call", fake_call)
    monkeypatch.setattr("loopcore.planner_adapter.time.sleep", lambda _: None)
    action = planner.plan(_audit(), {"task_id": "TASK-1"}, "ACT-1")
    assert action.action == PlannerActionType.CANDIDATE_DONE


def test_replan_with_nonempty_replacement_objective_is_valid(monkeypatch):
    planner = CodexCliPlannerProvider()
    monkeypatch.setattr(
        planner, "_call",
        lambda *a, **k: _replan_action("unused") | {
            "replacement_task_spec": {
                "objective": "  Use a corrected implementation route  "}})

    action = planner.plan(
        _replan_audit(), {"task_id": "TASK-1"}, "ACT-REPLAN",
        target_session_id="worker-old", remaining_replans=1)

    assert action.action == PlannerActionType.REPLAN_SPAWN
    assert action.replacement_task_spec == {
        "objective": "Use a corrected implementation route"}
    assert action.validate()[0]


@pytest.mark.parametrize("invalid_replacement", [
    None,
    {},
    {"objective": "   "},
])
def test_replan_invalid_replacement_retries_then_succeeds(
        monkeypatch, invalid_replacement):
    planner = CodexCliPlannerProvider()
    outputs = iter([
        _replan_action(invalid_replacement),
        _replan_action({"objective": "Use the corrected route"}),
    ])
    calls = []

    def fake_call(*args, **kwargs):
        calls.append(1)
        return next(outputs)

    monkeypatch.setattr(planner, "_call", fake_call)
    monkeypatch.setattr("loopcore.planner_adapter.time.sleep", lambda _: None)

    action = planner.plan(
        _replan_audit(), {"task_id": "TASK-1"}, "ACT-REPLAN",
        target_session_id="worker-old", remaining_replans=1)

    assert len(calls) == 2
    assert action.action == PlannerActionType.REPLAN_SPAWN
    assert action.replacement_task_spec["objective"] == \
        "Use the corrected route"


def test_replan_two_invalid_replacements_fail_closed_to_human(monkeypatch):
    planner = CodexCliPlannerProvider()
    outputs = iter([
        _replan_action(None),
        _replan_action({}),
    ])
    monkeypatch.setattr(planner, "_call", lambda *a, **k: next(outputs))
    monkeypatch.setattr("loopcore.planner_adapter.time.sleep", lambda _: None)

    action = planner.plan(
        _replan_audit(), {"task_id": "TASK-1"}, "ACT-REPLAN",
        target_session_id="worker-old", remaining_replans=1)

    assert action.action == PlannerActionType.HUMAN
    assert "replacement_task_spec.objective" in action.reason


def test_plan_fails_closed_to_human_after_two_failures(monkeypatch):
    planner = CodexCliPlannerProvider()
    monkeypatch.setattr(
        planner, "_call", lambda *a, **k: (_ for _ in ()).throw(
            CodexCliError("offline failure")))
    monkeypatch.setattr("loopcore.planner_adapter.time.sleep", lambda _: None)
    action = planner.plan(_audit(), {"task_id": "TASK-1"}, "ACT-1")
    assert action.action == PlannerActionType.HUMAN
    assert "offline failure" in action.reason


def test_decompose_raises_after_two_failures(monkeypatch):
    planner = CodexCliPlannerProvider()
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise CodexCliError("offline failure")

    monkeypatch.setattr(planner, "_call_decompose", fail)
    monkeypatch.setattr("loopcore.planner_adapter.time.sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="failed twice"):
        planner.plan_decompose(MISSION, "DECOMP-M-CODEX")
    assert len(calls) == 2


def test_decompose_retries_once_then_succeeds(monkeypatch):
    planner = CodexCliPlannerProvider()
    values = iter([CodexCliError("first"), _plan()])

    def fake_call(*args, **kwargs):
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(planner, "_call_decompose", fake_call)
    monkeypatch.setattr("loopcore.planner_adapter.time.sleep", lambda _: None)
    plan = planner.plan_decompose(MISSION, "DECOMP-M-CODEX")
    assert plan.mission_id == "M-CODEX"


def test_build_runtime_uses_codex_planner_and_config_model(monkeypatch,
                                                          tmp_path):
    adapter_calls = []
    executor_calls = []

    class DummyStore:
        def __init__(self, path):
            self.path = path

        def close(self):
            pass

    class DummyMemory:
        def __init__(self, path):
            self.memory_path = Path(path) / "memory.md"
            self.project_path = Path(path) / "project.md"

    class DummyController:
        def __init__(self, mission, cfg, **kwargs):
            self.state = "MISSION_READY"
            self.planner = kwargs["planner"]
            self.auditor = kwargs["auditor"]
            self.verifier = kwargs["verifier"]

    class DummyProjector:
        def __init__(self, *args, **kwargs):
            self.projected = []
            self.errors = []

    class DummyAdapter:
        def __init__(self, **kwargs):
            adapter_calls.append(kwargs)
            self.base_url = kwargs["base_url"]

    def dummy_executor(**kwargs):
        executor_calls.append(kwargs)
        return object()

    monkeypatch.setattr(run_mission, "ROOT", tmp_path)
    monkeypatch.setattr(run_mission, "resolve_ao_bin",
                        lambda: str(tmp_path / "ao.exe"))
    monkeypatch.setattr(run_mission, "resolve_ao_run_file",
                        lambda: tmp_path / "running.json")
    monkeypatch.setattr(
        run_mission, "mission_preflight",
        lambda *_args, **_kwargs: {
            "ao_bin": str(tmp_path / "ao.exe"),
            "ao_run_file": tmp_path / "running.json",
            "project_path": tmp_path})
    monkeypatch.setattr(run_mission, "StateStore", DummyStore)
    monkeypatch.setattr(run_mission, "AOAdapter", DummyAdapter)
    monkeypatch.setattr(run_mission, "ActionExecutor", dummy_executor)
    monkeypatch.setattr(run_mission, "IntegrationGate", lambda store: object())
    monkeypatch.setattr(run_mission, "MissionController", DummyController)
    monkeypatch.setattr(run_mission, "LoopBus", lambda config: object())
    monkeypatch.setattr(run_mission, "ProjectMemory", DummyMemory)
    monkeypatch.setattr(run_mission, "StoreBusProjector", DummyProjector)

    cfg = {
        "ao": {
            "base_url": "http://127.0.0.1:4111",
            "request_timeout_seconds": 7,
        },
        "roles": {
            "planner": {"model": "planner-model"},
            "auditor": {"model": "auditor-model"},
            "verifier": {"model": "verifier-model"},
        },
    }
    runtime = run_mission.build_runtime(MISSION, cfg)

    assert isinstance(runtime._planner, CodexCliPlannerProvider)
    assert isinstance(runtime._auditor, CodexCliAuditorProvider)
    assert isinstance(runtime._verifier, CodexCliVerifierProvider)
    assert runtime._planner.model == "planner-model"
    assert runtime._auditor.model == "auditor-model"
    assert runtime._verifier.model == "verifier-model"
    assert runtime._planner.timeout == 180
    assert runtime._auditor.timeout == 180
    assert runtime._verifier.timeout == 180
    assert runtime.controller.planner is runtime._planner
    assert runtime.controller.auditor is runtime._auditor
    assert runtime.controller.verifier is runtime._verifier
    assert runtime.ao_bin == str(tmp_path / "ao.exe")
    assert runtime.ao_run_file == str(tmp_path / "running.json")
    assert adapter_calls[0] == {
        "base_url": "http://127.0.0.1:4111",
        "timeout": 7.0,
        "run_file": tmp_path / "running.json",
    }
    assert executor_calls[0]["ao_bin"] == str(tmp_path / "ao.exe")
    assert executor_calls[0]["data_dir"] is None
    assert executor_calls[0]["run_file"] == str(tmp_path / "running.json")

    fallback = run_mission.build_runtime(MISSION, {})
    assert fallback._planner.model == "gpt-5.6-sol"
    assert fallback._auditor.model == "gpt-5.6-sol"
    assert fallback._verifier.model == "gpt-5.6-sol"


def test_build_planner_uses_default_model_when_config_missing():
    planner = run_mission.build_planner({})
    assert planner.model == "gpt-5.6-sol"


def test_codex_planner_does_not_set_anthropic_model(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    CodexCliPlannerProvider()
    assert "ANTHROPIC_MODEL" not in os.environ


def test_dry_run_outputs_plan_without_runtime_or_ao(monkeypatch, tmp_path,
                                                    capsys):
    mission_path = tmp_path / "mission.json"
    mission_path.write_text(json.dumps(MISSION), encoding="utf-8")

    monkeypatch.setattr(run_mission, "load_config", lambda: {
        "roles": {"planner": {"model": "configured-model"}}})
    monkeypatch.setattr(run_mission, "ROOT", tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run instantiated a forbidden runtime part")

    for name in ("build_planner", "setup_environment", "build_runtime",
                 "MissionRuntime",
                 "StateStore", "AOAdapter", "ActionExecutor",
                 "CodexCliAuditorProvider", "CodexCliVerifierProvider",
                 "IntegrationGate", "LoopBus"):
        monkeypatch.setattr(run_mission, name, forbidden)
    monkeypatch.setattr(sys, "argv", ["run_mission.py", str(mission_path),
                                      "--dry-run"])

    assert run_mission.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mission_id"] == "M-CODEX"
    assert output["dry_run"] is True
    assert output["planner_provider"] is None
    assert output["model"] is None
    assert output["subtask_count"] == 1
    assert output["plan"]["subtasks"][0]["subtask_id"] == "M-CODEX-S1"
    assert not (tmp_path / "runtime").exists()


def test_dry_run_provider_failure_is_brief_and_returns_two(monkeypatch,
                                                           tmp_path, capsys):
    mission_path = tmp_path / "mission.json"
    mission = dict(MISSION, budgets={"max_subtasks": 2})
    mission_path.write_text(json.dumps(mission), encoding="utf-8")

    class FailingPlanner:
        model = "gpt-5.6-sol"

        def plan_decompose(self, mission, plan_id):
            raise RuntimeError("planner failed")

    monkeypatch.setattr(run_mission, "load_config", lambda: {})
    monkeypatch.setattr(run_mission, "build_planner",
                        lambda *a, **k: FailingPlanner())
    monkeypatch.setattr(sys, "argv", ["run_mission.py", str(mission_path),
                                      "--dry-run"])

    assert run_mission.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "planning dry-run error: planner failed" in captured.err


def test_dry_run_rejects_invalid_mission_before_building_planner(
        monkeypatch, tmp_path, capsys):
    mission_path = tmp_path / "mission.json"
    invalid = dict(MISSION, allowed_paths="app.py")
    mission_path.write_text(json.dumps(invalid), encoding="utf-8")
    monkeypatch.setattr(run_mission, "load_config", lambda: {})
    monkeypatch.setattr(
        run_mission, "build_planner",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("Planner must not be built for an invalid mission")))
    monkeypatch.setattr(sys, "argv", ["run_mission.py", str(mission_path),
                                      "--dry-run"])

    assert run_mission.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "allowed_paths must be a list" in captured.err
