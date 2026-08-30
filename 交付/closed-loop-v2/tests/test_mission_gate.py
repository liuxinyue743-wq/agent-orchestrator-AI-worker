"""Regression (review 簇三): the Integration Gate must not use a shell, and
must detect when running the gate MUTATED the worktree HEAD (which
invalidates 'diff vs frozen base' evidence handed to the Verifier).
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from loopcore.action_executor import ActionExecutor
from loopcore.auditor import FakeAuditorProvider
from loopcore.closed_loop import ClosedLoop
from loopcore.event_observer import Observer
from loopcore.mission_contracts import (ProjectState, TaskSpec,
                                        VerifierResult)
from loopcore.mission_gate import IntegrationGate, _to_argv
from loopcore.planner_adapter import FakePlannerProvider
from loopcore.state_store import StateStore
from loopcore.verifier import FakeVerifierProvider
from tests.sidecar_port.test_phase3 import _cfg
from tests.sidecar_port.test_contracts import _task_spec


def _repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(path),
                   check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(path),
                   check=True)
    (path / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(path),
                   check=True)


def test_shell_metacharacters_are_not_interpreted(tmp_path):
    """`python -c "pass" > injected.txt` must NOT create injected.txt —
    with shell=False the '>' is a literal argv token, not redirection."""
    _repo(tmp_path / "wt")
    wt = tmp_path / "wt"
    task = TaskSpec.from_dict(_task_spec())
    task.gate_commands = ['python -c "pass" > injected.txt']
    store = StateStore(str(tmp_path / "cl.db"))
    run = IntegrationGate(store).run(task, str(wt))
    assert run.results[0]["exit_code"] == 0  # python ran, ignored extra args
    assert not (wt / "injected.txt").exists()  # no shell -> no redirection


def test_unparseable_command_fails_closed(tmp_path):
    _repo(tmp_path / "wt")
    task = TaskSpec.from_dict(_task_spec())
    task.gate_commands = ['python -c "unterminated']
    store = StateStore(str(tmp_path / "cl.db"))
    run = IntegrationGate(store).run(task, str(tmp_path / "wt"))
    assert run.ok is False
    assert run.results[0]["exit_code"] == -1


def test_head_mutation_is_detected(tmp_path):
    """A gate command that commits moves HEAD -> mutation flag + evidence."""
    wt = tmp_path / "wt"
    _repo(wt)
    task = TaskSpec.from_dict(_task_spec())
    task.gate_commands = ['git commit --allow-empty -qm mutation']
    store = StateStore(str(tmp_path / "cl.db"))
    run = IntegrationGate(store).run(task, str(wt))
    assert run.ok is True
    assert run.head_mutated is True
    assert run.head_before != run.head_after
    assert any(e["type"] == "gate_head_mutation" for e in run.evidence())


def test_clean_gate_has_no_mutation(tmp_path):
    wt = tmp_path / "wt"
    _repo(wt)
    task = TaskSpec.from_dict(_task_spec())
    task.gate_commands = ['python -c "pass"']
    store = StateStore(str(tmp_path / "cl.db"))
    run = IntegrationGate(store).run(task, str(wt))
    assert run.ok is True and run.head_mutated is False


class _RecordingVerifier:
    """Captures the VerifierInput, then FAILs so the loop stops there."""

    def __init__(self):
        self.inputs = []

    def verify(self, inp, verify_id):
        self.inputs.append(inp)
        return VerifierResult(verify_id=verify_id,
                              task_id=inp.task_spec.get("task_id", ""),
                              verdict="FAIL", ac_checks=[], anti_gaming=[],
                              summary="recorded")


def test_head_mutation_reaches_verifier_findings(tmp_path, monkeypatch):
    """Loop-level: a mutating gate must surface a deterministic finding."""
    store = StateStore(str(tmp_path / "cl.db"))
    task = TaskSpec.from_dict(_task_spec())
    task.worker_session_id = "w-mut"
    task.gate_commands = ['git commit --allow-empty -qm mutation']
    wt = tmp_path / "worktrees" / task.project_id / task.worker_session_id
    _repo(wt)
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path))
    adapter = MagicMock()
    adapter.get_worker_status.return_value = {"id": task.worker_session_id,
                                              "status": "idle"}
    verifier = _RecordingVerifier()
    loop = ClosedLoop(task=task, cfg=_cfg(), auditor=FakeAuditorProvider(),
                      planner=FakePlannerProvider(),
                      executor=ActionExecutor("ao", "d", "r", store),
                      observer=Observer(_cfg(), state_store=store),
                      adapter=adapter, gate=IntegrationGate(store),
                      store=store, verifier=verifier)
    loop._transition(ProjectState.WORKER_RUNNING, "test", "setup", {})
    loop._transition(ProjectState.GATE_PENDING, "test", "setup", {})
    loop._run_gate()
    assert verifier.inputs, "verifier must have been invoked"
    findings = verifier.inputs[0].deterministic_findings
    assert any("mutated HEAD" in f for f in findings), findings


def test_to_argv_windows_quote_stripping():
    argv = _to_argv('python -m pytest "tests/my dir" -q')
    assert argv[0].lower().endswith("python.exe") or argv[0] == "python"
    assert argv[1:3] == ["-m", "pytest"]
    assert argv[-1] == "-q"
    assert not any(a.startswith('"') for a in argv)
