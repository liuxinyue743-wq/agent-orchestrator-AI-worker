"""Offline Codex Verifier migration tests."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from loopcore.codex_cli import CodexCliError
from loopcore.verifier import CodexCliVerifierProvider, VerifierInput


def test_verifier_provider_does_not_set_anthropic_model(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

    CodexCliVerifierProvider()

    assert "ANTHROPIC_MODEL" not in os.environ


def _input():
    return VerifierInput(
        task_spec={
            "task_id": "TASK-VERIFIER",
            "objective": "Implement divide",
            "acceptance_criteria": [
                {"id": "AC-1", "description": "divide works"}],
        },
        diff="diff --git a/app.py b/app.py\n+def divide(a, b): return a / b",
        gate_output="1 passed",
        changed_paths=["app.py"],
        deterministic_findings=[],
    )


def _result(verdict="PASS"):
    return {
        "verify_id": "VERIFY-CODEX",
        "task_id": "TASK-VERIFIER",
        "verdict": verdict,
        "ac_checks": [{
            "ac_id": "AC-1",
            "verdict": "PASS" if verdict == "PASS" else "FAIL",
            "note": "checked diff and gate output",
        }],
        "anti_gaming": [{
            "ac_id": "tests-untouched",
            "verdict": "PASS",
            "note": "tests were not changed",
        }],
        "summary": "verified" if verdict == "PASS" else "failed AC-1",
    }


@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
def test_verify_uses_shared_runner_and_returns_valid_result(
        monkeypatch, tmp_path, verdict):
    from loopcore import verifier
    calls = []
    monkeypatch.setattr(
        verifier, "run_codex_json",
        lambda **kwargs: calls.append(kwargs) or _result(verdict))
    provider = CodexCliVerifierProvider(
        codex_bin="codex-v", model="model-v", timeout=41, cwd=tmp_path)

    result = provider.verify(_input(), "VERIFY-CODEX")

    assert result.verdict == verdict
    assert len(calls) == 1
    call = calls[0]
    assert Path(call["schema_path"]).name == "verifier-result.schema.json"
    assert call["model"] == "model-v"
    assert call["timeout"] == 41
    assert call["codex_bin"] == "codex-v"
    assert call["cwd"] == tmp_path
    assert provider.system_prompt in call["prompt"]
    assert "VerifierInput" in call["prompt"]
    assert '"task_id": "TASK-VERIFIER"' in call["prompt"]
    assert "diff --git a/app.py" in call["prompt"]


def test_verify_transport_failure_propagates_without_retry(monkeypatch):
    provider = CodexCliVerifierProvider()
    calls = []

    def fake_call(*args, **kwargs):
        calls.append(1)
        raise CodexCliError("timeout")

    monkeypatch.setattr(provider, "_call", fake_call)
    with pytest.raises(CodexCliError, match="timeout"):
        provider.verify(_input(), "VERIFY-CODEX")
    assert len(calls) == 1


def test_invalid_verifier_result_is_retried(monkeypatch):
    provider = CodexCliVerifierProvider()
    values = iter([{}, _result("FAIL")])
    calls = []

    def fake_call(*args, **kwargs):
        calls.append(1)
        return next(values)

    monkeypatch.setattr(provider, "_call", fake_call)
    monkeypatch.setattr("loopcore.verifier.time.sleep", lambda _: None)
    assert provider.verify(_input(), "VERIFY-CODEX").verdict == "FAIL"
    assert len(calls) == 2


def test_verifier_coerce_still_applies_before_local_validation(monkeypatch):
    provider = CodexCliVerifierProvider()
    raw = _result()
    raw["summary"] = None
    raw["anti_gaming"][0]["note"] = None
    raw["anti_gaming"][0]["verdict"] = "UNKNOWN"
    monkeypatch.setattr(provider, "_call", lambda *a, **k: raw)

    result = provider.verify(_input(), "VERIFY-CODEX")

    assert result.summary == ""
    assert result.anti_gaming[0].note == ""
    assert result.anti_gaming[0].verdict == "UNVERIFIABLE"


def test_two_schema_invalid_verifier_results_raise_protocol_error(monkeypatch):
    provider = CodexCliVerifierProvider()
    calls = []

    def invalid(*args, **kwargs):
        calls.append(1)
        return {}

    monkeypatch.setattr(provider, "_call", invalid)
    monkeypatch.setattr("loopcore.verifier.time.sleep", lambda _: None)
    with pytest.raises(CodexCliError, match="schema-invalid output twice"):
        provider.verify(_input(), "VERIFY-CODEX")
    assert len(calls) == 2


def test_valid_semantic_fail_remains_fail(monkeypatch):
    provider = CodexCliVerifierProvider()
    monkeypatch.setattr(provider, "_call", lambda *a, **k: _result("FAIL"))
    result = provider.verify(_input(), "VERIFY-CODEX")
    assert result.verdict == "FAIL"
    assert result.summary == "failed AC-1"
