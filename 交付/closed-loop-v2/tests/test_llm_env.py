"""llm_env: providers must self-sufficiently set the claude CLI env
(CLAUDE_CODE_GIT_BASH_PATH / ANTHROPIC_MODEL) without depending on
run_mission.py process setup — but never override operator values."""

from __future__ import annotations


def test_ensure_llm_env_sets_defaults(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_GIT_BASH_PATH", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    from loopcore import llm_env
    monkeypatch.setattr(llm_env, "find_git_bash", lambda: r"C:\git\bash.exe")
    llm_env.ensure_llm_env()
    import os
    assert os.environ["CLAUDE_CODE_GIT_BASH_PATH"] == r"C:\git\bash.exe"
    assert os.environ["ANTHROPIC_MODEL"] == llm_env.DEFAULT_MODEL


def test_ensure_llm_env_never_overrides_operator(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", r"D:\mine\bash.exe")
    monkeypatch.setenv("ANTHROPIC_MODEL", "custom-model")
    from loopcore import llm_env
    monkeypatch.setattr(llm_env, "find_git_bash", lambda: r"C:\git\bash.exe")
    llm_env.ensure_llm_env()
    import os
    assert os.environ["CLAUDE_CODE_GIT_BASH_PATH"] == r"D:\mine\bash.exe"
    assert os.environ["ANTHROPIC_MODEL"] == "custom-model"


def test_providers_wire_ensure_env(monkeypatch):
    """All three CLI providers must call ensure_llm_env on construction."""
    import os
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    from loopcore.auditor import ClaudeCliAuditorProvider
    from loopcore.verifier import ClaudeCliVerifierProvider
    from loopcore.planner_adapter import AOOrchestratorPlannerProvider
    ClaudeCliAuditorProvider()
    assert os.environ.get("ANTHROPIC_MODEL")
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    ClaudeCliVerifierProvider()
    assert os.environ.get("ANTHROPIC_MODEL")
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    AOOrchestratorPlannerProvider()
    assert os.environ.get("ANTHROPIC_MODEL")
