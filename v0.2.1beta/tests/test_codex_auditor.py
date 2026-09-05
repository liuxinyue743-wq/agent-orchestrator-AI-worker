"""Offline Codex Auditor migration tests."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from loopcore.auditor import (
    CodexCliAuditorProvider,
    EvidenceBundle,
    FakeAuditorProvider,
)
from loopcore.codex_cli import CodexCliError
from loopcore.mission_contracts import AuditDecision


def test_auditor_provider_does_not_set_anthropic_model(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

    CodexCliAuditorProvider()

    assert "ANTHROPIC_MODEL" not in os.environ


def _bundle(failed=None):
    return EvidenceBundle(
        task_spec={
            "task_id": "TASK-AUDITOR",
            "objective": "Implement divide",
            "acceptance_criteria": [
                {"id": "AC-1", "description": "divide works"}],
            "budgets": {"max_local_fixes": 2, "max_replans": 1},
        },
        alert={"alert_type": "REPEATED_ERROR"},
        test_output="AssertionError: divide is not implemented",
        failed_criteria=list(failed or []),
        history={"local_fixes": 0, "replans": 0},
    )


def _result(decision="LOCAL_FIX"):
    return {
        "audit_id": "AUD-CODEX",
        "task_id": "TASK-AUDITOR",
        "decision": decision,
        "failed_criteria": [] if decision == "PASS" else ["AC-1"],
        "evidence": [{
            "type": "test_failure" if decision != "PASS" else "test_pass",
            "summary": "deterministic evidence is present",
            "reference": "test_output",
        }],
        "diagnosis": "complete" if decision == "PASS" else "local fix needed",
        "recommended_action": "" if decision == "PASS" else "implement divide",
        "confidence": 0.95,
    }


@pytest.mark.parametrize("decision", ["LOCAL_FIX", "PASS"])
def test_audit_uses_shared_runner_and_returns_valid_result(
        monkeypatch, tmp_path, decision):
    from loopcore import auditor
    calls = []
    monkeypatch.setattr(
        auditor, "run_codex_json",
        lambda **kwargs: calls.append(kwargs) or _result(decision))
    provider = CodexCliAuditorProvider(
        codex_bin="codex-a", model="model-a", timeout=37, cwd=tmp_path)

    result = provider.audit(_bundle(["AC-1"]), "AUD-CODEX")

    assert result.decision == decision
    assert result.validate()[0]
    assert len(calls) == 1
    call = calls[0]
    assert Path(call["schema_path"]).name == "audit-result.schema.json"
    assert call["model"] == "model-a"
    assert call["timeout"] == 37
    assert call["codex_bin"] == "codex-a"
    assert call["cwd"] == tmp_path
    assert provider.system_prompt in call["prompt"]
    assert "EvidenceBundle" in call["prompt"]
    assert '"task_id": "TASK-AUDITOR"' in call["prompt"]
    assert "AssertionError: divide is not implemented" in call["prompt"]


def test_audit_transport_failure_propagates_without_retry(monkeypatch):
    provider = CodexCliAuditorProvider()
    calls = []

    def fake_call(*args, **kwargs):
        calls.append(1)
        raise CodexCliError("timeout")

    monkeypatch.setattr(provider, "_call", fake_call)
    with pytest.raises(CodexCliError, match="timeout"):
        provider.audit(_bundle(["AC-1"]), "AUD-CODEX")
    assert len(calls) == 1


def test_invalid_audit_result_is_retried(monkeypatch):
    provider = CodexCliAuditorProvider()
    values = iter([{"decision": "BOGUS", "evidence": []}, _result("PASS")])
    calls = []

    def fake_call(*args, **kwargs):
        calls.append(1)
        return next(values)

    monkeypatch.setattr(provider, "_call", fake_call)
    monkeypatch.setattr("loopcore.auditor.time.sleep", lambda _: None)
    assert provider.audit(_bundle(), "AUD-CODEX").decision == \
        AuditDecision.PASS
    assert len(calls) == 2


def test_two_schema_invalid_audit_results_raise_protocol_error(monkeypatch):
    provider = CodexCliAuditorProvider()
    calls = []

    def invalid(*args, **kwargs):
        calls.append(1)
        return {"decision": "BOGUS", "evidence": []}

    monkeypatch.setattr(provider, "_call", invalid)
    monkeypatch.setattr("loopcore.auditor.time.sleep", lambda _: None)
    with pytest.raises(CodexCliError, match="schema-invalid output twice"):
        provider.audit(_bundle(["AC-1"]), "AUD-CODEX")
    assert len(calls) == 2


def test_valid_semantic_human_remains_human(monkeypatch):
    provider = CodexCliAuditorProvider()
    monkeypatch.setattr(provider, "_call", lambda *a, **k: _result("HUMAN"))
    result = provider.audit(_bundle(["AC-1"]), "AUD-CODEX")
    assert result.decision == AuditDecision.HUMAN
    assert result.diagnosis == "local fix needed"


def test_fake_auditor_behavior_is_unchanged():
    fake = FakeAuditorProvider()
    assert fake.audit(_bundle(["AC-1"]), "A1").decision == \
        AuditDecision.LOCAL_FIX
    assert fake.audit(_bundle(), "A2").decision == AuditDecision.PASS
