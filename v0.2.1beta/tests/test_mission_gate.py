"""Regression (review 簇三): the Integration Gate must not use a shell, and
must detect when running the gate MUTATED the worktree HEAD (which
invalidates 'diff vs frozen base' evidence handed to the Verifier).
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from loopcore import worktree as wt
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


def _script_command(path) -> str:
    return '"%s" "%s"' % (sys.executable, path)


def _write_script(repo, name, source):
    script = repo / name
    script.write_text(source, encoding="utf-8")
    return _script_command(script)


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
    assert run.ok is False
    assert run.command_ok is True
    assert run.integrity_ok is False
    assert run.integrity_error == "Gate changed HEAD"
    assert run.head_mutated is True
    assert run.head_before != run.head_after
    assert any(e["type"] == "gate_repository_integrity"
               for e in run.evidence())


def test_clean_gate_has_no_mutation(tmp_path):
    wt = tmp_path / "wt"
    _repo(wt)
    task = TaskSpec.from_dict(_task_spec())
    task.gate_commands = ['python -c "pass"']
    store = StateStore(str(tmp_path / "cl.db"))
    run = IntegrationGate(store).run(task, str(wt))
    assert run.ok is True and run.head_mutated is False
    assert run.command_ok is True and run.integrity_ok is True
    assert run.state_digest_before == run.state_digest_after


def test_pre_gate_probe_failure_skips_commands(tmp_path):
    repo = tmp_path / "wt"
    _repo(repo)
    task = TaskSpec.from_dict(_task_spec())
    task.gate_commands = [_write_script(
        repo, "would_run.py",
        "from pathlib import Path\nPath('command-ran').write_text('yes')\n")]
    store = StateStore(str(tmp_path / "cl.db"))

    with patch("loopcore.mission_gate.wt.git_state_snapshot",
               side_effect=wt.GitStateSnapshotError("HEAD unavailable")):
        run = IntegrationGate(store).run(task, str(repo))

    assert run.ok is False
    assert run.command_ok is False
    assert run.integrity_error.startswith("pre-Gate Git probe failed")
    assert run.results == []
    assert not (repo / "command-ran").exists()


def test_post_gate_probe_failure_fails_closed(tmp_path):
    repo = tmp_path / "wt"
    _repo(repo)
    task = TaskSpec.from_dict(_task_spec())
    task.gate_commands = [_write_script(
        repo, "ran.py",
        "from pathlib import Path\nPath('.coverage').write_text('ran')\n")]
    store = StateStore(str(tmp_path / "cl.db"))
    before = wt.git_state_snapshot(str(repo))

    with patch("loopcore.mission_gate.wt.git_state_snapshot",
               side_effect=[before,
                            wt.GitStateSnapshotError("diff unavailable")]):
        run = IntegrationGate(store).run(task, str(repo))

    assert run.ok is False
    assert run.command_ok is True
    assert run.integrity_error.startswith("post-Gate Git probe failed")
    assert run.results[0]["exit_code"] == 0
    assert (repo / ".coverage").exists()


def test_initial_dirty_task_gate_can_pass_when_unchanged(tmp_path):
    repo = tmp_path / "wt"
    _repo(repo)
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    task = TaskSpec.from_dict(_task_spec())
    task.gate_commands = ['python -c "pass"']

    run = IntegrationGate(StateStore(str(tmp_path / "cl.db"))).run(
        task, str(repo))

    assert run.ok is True
    assert run.initial_clean is False
    assert run.state_digest_before == run.state_digest_after


def test_gate_mutating_already_dirty_file_is_content_sensitive(tmp_path):
    repo = tmp_path / "wt"
    _repo(repo)
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    task = TaskSpec.from_dict(_task_spec())
    task.gate_commands = [_write_script(
        repo, "mutate.py",
        "from pathlib import Path\n"
        "p = Path('app.py')\n"
        "p.write_text(p.read_text() + 'gate = True\\n')\n")]

    run = IntegrationGate(StateStore(str(tmp_path / "cl.db"))).run(
        task, str(repo))

    assert run.command_ok is True
    assert run.integrity_ok is False
    assert run.integrity_error == "Gate changed repository state"
    assert run.state_digest_before != run.state_digest_after


def test_gate_creating_non_artifact_file_fails_integrity(tmp_path):
    repo = tmp_path / "wt"
    _repo(repo)
    task = TaskSpec.from_dict(_task_spec())
    task.gate_commands = [_write_script(
        repo, "create.py",
        "from pathlib import Path\nPath('generated.txt').write_text('new')\n")]

    run = IntegrationGate(StateStore(str(tmp_path / "cl.db"))).run(
        task, str(repo))

    assert run.command_ok is True
    assert run.integrity_ok is False
    assert run.integrity_error == "Gate changed repository state"


def test_gate_mutating_existing_untracked_content_fails_integrity(tmp_path):
    repo = tmp_path / "wt"
    _repo(repo)
    (repo / "notes.txt").write_text("before", encoding="utf-8")
    task = TaskSpec.from_dict(_task_spec())
    task.gate_commands = [_write_script(
        repo, "mutate_untracked.py",
        "from pathlib import Path\nPath('notes.txt').write_text('after')\n")]

    run = IntegrationGate(StateStore(str(tmp_path / "cl.db"))).run(
        task, str(repo))

    assert run.command_ok is True
    assert run.integrity_ok is False
    assert run.state_digest_before != run.state_digest_after


def test_gate_only_creating_artifacts_preserves_integrity(tmp_path):
    repo = tmp_path / "wt"
    _repo(repo)
    task = TaskSpec.from_dict(_task_spec())
    task.gate_commands = [_write_script(
        repo, "artifacts.py",
        "from pathlib import Path\n"
        "Path('__pycache__').mkdir()\n"
        "Path('__pycache__/cache.pyc').write_bytes(b'cache')\n"
        "Path('.pytest_cache').mkdir()\n"
        "Path('.pytest_cache/state').write_text('cache')\n"
        "Path('.coverage').write_text('coverage')\n")]

    run = IntegrationGate(StateStore(str(tmp_path / "cl.db"))).run(
        task, str(repo))

    assert run.ok is True
    assert run.integrity_ok is True
    assert run.state_digest_before == run.state_digest_after


@pytest.mark.parametrize("operation", ["stage", "unstage"])
def test_gate_stage_or_unstage_source_fails_integrity(tmp_path, operation):
    repo = tmp_path / "wt"
    _repo(repo)
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    if operation == "unstage":
        subprocess.run(["git", "add", "app.py"], cwd=str(repo), check=True)
    argv = (["git", "add", "app.py"] if operation == "stage" else
            ["git", "reset", "-q", "--", "app.py"])
    task = TaskSpec.from_dict(_task_spec())
    task.gate_commands = [_write_script(
        repo, "stage.py",
        "import subprocess\nsubprocess.run(%r, check=True)\n" % argv)]

    run = IntegrationGate(StateStore(str(tmp_path / "cl.db"))).run(
        task, str(repo))

    assert run.command_ok is True
    assert run.integrity_ok is False


@pytest.mark.parametrize("operation", ["delete", "rename"])
def test_gate_delete_or_rename_source_fails_integrity(tmp_path, operation):
    repo = tmp_path / "wt"
    _repo(repo)
    source = ("Path('app.py').unlink()" if operation == "delete" else
              "Path('app.py').rename('renamed.py')")
    task = TaskSpec.from_dict(_task_spec())
    task.gate_commands = [_write_script(
        repo, "change_path.py", "from pathlib import Path\n" + source + "\n")]

    run = IntegrationGate(StateStore(str(tmp_path / "cl.db"))).run(
        task, str(repo))

    assert run.command_ok is True
    assert run.integrity_ok is False


def test_require_clean_rejects_initial_dirty_without_running_commands(tmp_path):
    repo = tmp_path / "wt"
    _repo(repo)
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    task = TaskSpec.from_dict(_task_spec())
    task.gate_commands = [_write_script(
        repo, "would_run.py",
        "from pathlib import Path\nPath('command-ran').write_text('yes')\n")]

    run = IntegrationGate(StateStore(str(tmp_path / "cl.db"))).run(
        task, str(repo), require_clean=True)

    assert run.ok is False
    assert run.integrity_ok is False
    assert run.integrity_error == "initial repository not clean"
    assert run.initial_clean is False
    assert run.results == []
    assert not (repo / "command-ran").exists()


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
    """Historical task verification preserves gate-mutation evidence."""
    store = StateStore(str(tmp_path / "cl.db"))
    task = TaskSpec.from_dict(_task_spec())
    task.worker_session_id = "w-mut"
    task.gate_commands = ['git commit --allow-empty -qm mutation']
    wt = tmp_path / "worktrees" / task.project_id / task.worker_session_id
    _repo(wt)
    adapter = MagicMock()
    adapter.get_session_workspace.return_value = str(wt)
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
    loop._transition(ProjectState.VERIFIER_PENDING, "test", "historical", {})
    loop._run_verifier()
    assert verifier.inputs, "verifier must have been invoked"
    findings = verifier.inputs[0].deterministic_findings
    assert any("mutated HEAD" in f for f in findings), findings


def test_to_argv_windows_quote_stripping():
    argv = _to_argv('python -m pytest "tests/my dir" -q')
    import shutil
    # Assert the actual contract: executable lookup honors PATH on both
    # Windows and Linux; an absolute Unix Python path is not a product bug.
    assert argv[0] == (shutil.which("python") or "python")
    assert argv[1:] == ["-m", "pytest", "tests/my dir", "-q"]
    assert argv[-1] == "-q"
    assert not any(a.startswith('"') for a in argv)
