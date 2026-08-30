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
import subprocess
import sys
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


def _kill_process_tree(proc: "subprocess.Popen") -> None:
    """Kill a process AND its descendants.

    On Windows, ``subprocess`` only kills the direct child (the claude.cmd
    shim) on timeout; the node runtime it launched keeps running as an
    orphan, leaking API connections/budget across repeated timeouts. We kill
    the whole tree explicitly.
    """
    pid = proc.pid
    if sys.platform == "win32":
        # /T = tree (kill children too), /F = force
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True, timeout=15,
        )
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except Exception:
                pass


def run_claude(cmd: list, *, input: str, timeout: float,
               encoding: str = "utf-8", errors: str = "replace",
               env=None) -> "subprocess.CompletedProcess":
    """Run the claude CLI with a timeout that kills the WHOLE process tree.

    A drop-in replacement for ``subprocess.run(cmd, input=..., timeout=...)``
    that, on timeout, kills the claude.cmd shim AND its node child (which
    ``subprocess.run`` leaves orphaned on Windows). Captures stdout/stderr as
    text. Raises ``subprocess.TimeoutExpired`` on timeout (after tree kill).
    """
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding=encoding,
            errors=errors, env=env,
        )
    except FileNotFoundError:
        raise
    try:
        stdout, stderr = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        # drain pipes so the child's fds are not inherited by leaked orphans
        try:
            proc.communicate(timeout=10)
        except Exception:
            pass
        raise
    return subprocess.CompletedProcess(
        args=cmd, returncode=proc.returncode,
        stdout=stdout or "", stderr=stderr or "",
    )
