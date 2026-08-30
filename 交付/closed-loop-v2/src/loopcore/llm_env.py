"""Shared environment for headless LLM CLI providers (auditor / planner /
verifier shelling out to the claude CLI).

Every process that invokes claude.cmd on Windows needs:
  - CLAUDE_CODE_GIT_BASH_PATH — claude.cmd requires Git Bash; without it the
    CLI dies with the 'Git for Windows' install error.
  - ANTHROPIC_MODEL — without an explicit model the gateway profile can fail
    with error_max_budget_usd (real-run evidence on the GLM gateway).

run_mission.py used to set both at process start, but every side entrance
(reverify scripts, ad-hoc audits, the future web panel) bypassed it and
broke. Now each provider calls ensure_llm_env() itself — idempotent, never
overrides an operator-supplied value.
"""

from __future__ import annotations

import os
from pathlib import Path

_GIT_BASH_CANDIDATES = [
    r"C:\Users\lenovo\AppData\Roaming\kimi-desktop\daimon-bundle"
    r"\runtime\git\usr\bin\bash.exe",
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
]

DEFAULT_MODEL = "GLM-5.2"


def find_git_bash() -> str:
    """First existing Git Bash path from the known install locations."""
    for cand in _GIT_BASH_CANDIDATES:
        if Path(cand).exists():
            return cand
    return ""


def ensure_llm_env(default_model: str = DEFAULT_MODEL) -> None:
    """Idempotently set the env the claude CLI needs. setdefault semantics:
    an operator-set value always wins."""
    if not os.environ.get("CLAUDE_CODE_GIT_BASH_PATH"):
        bash = find_git_bash()
        if bash:
            os.environ["CLAUDE_CODE_GIT_BASH_PATH"] = bash
    if default_model and not os.environ.get("ANTHROPIC_MODEL"):
        os.environ["ANTHROPIC_MODEL"] = default_model
