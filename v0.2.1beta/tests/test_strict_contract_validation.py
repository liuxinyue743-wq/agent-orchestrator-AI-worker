"""Full local validation regressions. No AO, models, credentials or network."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from loopcore.mission_contracts import (
    validate_audit_result, validate_planner_action,
    validate_task_spec, validate_verifier_result,
)
from tests.sidecar_port.test_contracts import _task_spec

ROOT = Path(__file__).resolve().parents[1]


def audit():
    return {
        "audit_id": "A1", "task_id": "T1", "decision": "LOCAL_FIX",
        "evidence": [{"type": "test_failure", "summary": "assertion failed"}],
        "diagnosis": "fix the implementation", "confidence": 0.8,
        "failed_criteria": ["AC-01"], "recommended_action": "fix app.py",
    }


def action():
    return {
        "action_id": "P1", "task_id": "T1", "action": "SEND_LOCAL_FIX",
        "reason": "failed criterion", "target_session_id": "w1",
        "message": "fix app.py", "replacement_task_spec": None,
    }


def verification():
    return {
        "verify_id": "V1", "task_id": "T1", "verdict": "PASS",
        "ac_checks": [{"ac_id": "AC-01", "verdict": "PASS", "note": "ok"}],
        "anti_gaming": [], "summary": "requirements met",
    }


@pytest.mark.parametrize("factory,validator", [
    (audit, validate_audit_result), (action, validate_planner_action),
    (_task_spec, validate_task_spec), (verification, validate_verifier_result),
])
def test_valid_contracts_remain_valid(factory, validator):
    ok, detail = validator(factory())
    assert ok, detail


@pytest.mark.parametrize("field,value", [
    ("evidence", "passed"), ("evidence", {"type": "test"}),
    ("evidence", []), ("evidence", ["not an object"]),
    ("evidence", [{"type": "test_failure"}]),
    ("evidence", [{"type": "test_failure", "summary": 123}]),
    ("evidence", [{"type": "test_failure", "summary": "x", "reference": []}]),
    ("confidence", 9), ("confidence", -0.1), ("confidence", "0.8"),
    ("confidence", True), ("confidence", float("nan")),
    ("confidence", float("inf")), ("confidence", float("-inf")),
    ("failed_criteria", [1]), ("decision", "APPROVE"), ("task_id", 12),
])
def test_audit_invalid_nested_types_and_ranges_rejected(field, value):
    obj = audit(); obj[field] = value
    ok, detail = validate_audit_result(obj)
    assert not ok and detail


@pytest.mark.parametrize("confidence", [0, 1, 0.5])
def test_valid_confidence_boundaries(confidence):
    obj = audit(); obj["confidence"] = confidence
    assert validate_audit_result(obj)[0]


@pytest.mark.parametrize("field,value", [
    ("target_session_id", ["w1"]), ("message", 17),
    ("replacement_task_spec", []), ("replacement_task_spec", {}),
    ("replacement_task_spec", {"objective": ""}),
    ("replacement_task_spec", {"objective": "x", "extra_control": True}),
])
def test_planner_nested_contracts_rejected(field, value):
    obj = action(); obj[field] = value
    assert not validate_planner_action(obj)[0]


@pytest.mark.parametrize("field,value", [
    ("allowed_paths", "app.py"), ("allowed_paths", [12]),
    ("acceptance_criteria", [{"id": "AC1"}]),
    ("gate_commands", [1]), ("gate_commands", []),
])
def test_task_wrong_types_rejected(field, value):
    obj = _task_spec(); obj[field] = value
    assert not validate_task_spec(obj)[0]


@pytest.mark.parametrize("field,value", [
    ("max_local_fixes", -1), ("max_replans", "1"),
    ("max_same_alerts", True), ("max_runtime_seconds", 0),
])
def test_nested_budget_constraints(field, value):
    obj = _task_spec(); obj["budgets"][field] = value
    assert not validate_task_spec(obj)[0]


@pytest.mark.parametrize("field,value", [
    ("ac_checks", "PASS"), ("ac_checks", {}), ("ac_checks", None),
    ("ac_checks", [{"ac_id": "AC1", "verdict": "UNKNOWN"}]),
    ("anti_gaming", "PASS"), ("anti_gaming", ["PASS"]),
])
def test_verifier_container_constraints(field, value):
    obj = verification(); obj[field] = value
    assert not validate_verifier_result(obj)[0]


@pytest.mark.parametrize("value", [None, [], "PASS", 42])
def test_scalar_or_array_root_rejected(value):
    assert not validate_audit_result(value)[0]


def test_missing_dependency_is_an_explicit_startup_failure():
    # Isolated child starts without pytest's imports. Block only jsonschema;
    # the production module must fail before it can validate any payload.
    code = '''
import builtins, sys
sys.path.insert(0, sys.argv[1])
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == "jsonschema" or name.startswith("jsonschema."):
        raise ModuleNotFoundError("blocked for dependency test", name="jsonschema")
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
import loopcore.mission_contracts
'''
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(ROOT / "src")],
        capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert "requires jsonschema" in result.stderr
    assert "bootstrap.ps1" in result.stderr


def test_dependency_bootstrap_and_launcher_are_wired():
    assert "jsonschema==4.26.0" in (ROOT / "requirements.txt").read_text("utf-8")
    assert "jsonschema" in (ROOT / "bootstrap.ps1").read_text("utf-8-sig")
    assert "jsonschema" in (ROOT / "启动CLAO.bat").read_text("utf-8-sig")


def test_local_schema_invalid_audit_is_not_returned_as_pass(monkeypatch):
    from loopcore.auditor import CodexCliAuditorProvider, EvidenceBundle
    from loopcore.codex_cli import CodexCliError
    provider = CodexCliAuditorProvider()
    calls = []
    def invalid(*_args, **_kwargs):
        calls.append(1)
        obj = audit(); obj["confidence"] = 9
        return obj
    monkeypatch.setattr(provider, "_call", invalid)
    monkeypatch.setattr("loopcore.auditor.time.sleep", lambda _: None)
    with pytest.raises(CodexCliError, match="schema-invalid"):
        provider.audit(EvidenceBundle(task_spec={"task_id": "T1"}, alert=None), "A1")
    assert len(calls) == 2


@pytest.mark.parametrize("field,value", [
    ("ac_checks", "PASS"), ("ac_checks", None),
    ("anti_gaming", {"verdict": "PASS"}), ("anti_gaming", [17]),
])
def test_verifier_does_not_silently_erase_malformed_evidence(monkeypatch, field, value):
    from loopcore.verifier import CodexCliVerifierProvider, VerifierInput
    from loopcore.codex_cli import CodexCliError
    provider = CodexCliVerifierProvider()
    calls = []
    def invalid(*_args, **_kwargs):
        calls.append(1)
        obj = verification(); obj[field] = copy.deepcopy(value)
        return obj
    monkeypatch.setattr(provider, "_call", invalid)
    monkeypatch.setattr("loopcore.verifier.time.sleep", lambda _: None)
    with pytest.raises(CodexCliError, match="schema-invalid"):
        provider.verify(VerifierInput(task_spec={"task_id": "T1"}), "V1")
    assert len(calls) == 2


@pytest.mark.parametrize("field,value", [
    ("allowed_paths", "app.py"), ("dependencies", "other-task"),
    ("gate_commands", "python -m pytest"),
    ("acceptance_criteria", [{"id": 12, "description": "works"}]),
    ("subtask_id", 12),
])
def test_mission_plan_nested_shapes_are_validated(field, value):
    from loopcore.planner_adapter import CodexCliPlannerProvider
    obj = {
        "mission_id": "M1", "subtasks": [{
            "subtask_id": "S1", "objective": "implement",
            "allowed_paths": ["app.py"], "acceptance_criteria": [],
        }],
    }
    obj["subtasks"][0][field] = value
    ok, _ = CodexCliPlannerProvider._validate_mission_plan(obj, 1)
    assert not ok
