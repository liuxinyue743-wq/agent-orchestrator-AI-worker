"""Regression (review 簇七 — Claude re-audit findings):
  HIGH   _run_verifier iterated changed_paths=None -> TypeError killed the
         runner (verified by reviewer). Now: None -> HUMAN, no exception.
  MEDIUM commit_all() claimed to exclude artifacts but `git add -A -- .`
         had no exclude pathspecs -> .pyc entered integration branches.
  LOW    idempotency ids were sliced from arbitrary strings/timestamps,
         producing 'L0-{"code":-326' and 'VERIFY-.485976+0000' — malformed
         and same-microsecond-collision-prone.
"""

from __future__ import annotations

import re
import subprocess
from unittest.mock import MagicMock

from loopcore.action_executor import ActionExecutor
from loopcore.auditor import FakeAuditorProvider
from loopcore.closed_loop import ClosedLoop
from loopcore.event_normalizer import make_id, stable_id
from loopcore.event_observer import Observer
from loopcore.mission_contracts import ProjectState, TaskSpec
from loopcore.mission_gate import IntegrationGate
from loopcore.planner_adapter import FakePlannerProvider
from loopcore.state_store import StateStore
from loopcore.verifier import FakeVerifierProvider
from loopcore import worktree as wt
from tests.sidecar_port.test_phase3 import _cfg
from tests.sidecar_port.test_contracts import _task_spec


def _make_loop(tmp_path, monkeypatch):
    store = StateStore(str(tmp_path / "cl.db"))
    task = TaskSpec.from_dict(_task_spec())
    task.worker_session_id = "w-audit7"
    wtdir = tmp_path / "worktrees" / task.project_id / task.worker_session_id
    wtdir.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(wtdir), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(wtdir),
                   check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(wtdir),
                   check=True)
    (wtdir / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=str(wtdir), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(wtdir),
                   check=True)
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path))
    adapter = MagicMock()
    adapter.get_worker_status.return_value = {"id": task.worker_session_id,
                                              "status": "idle"}
    loop = ClosedLoop(task=task, cfg=_cfg(), auditor=FakeAuditorProvider(),
                      planner=FakePlannerProvider(),
                      executor=ActionExecutor("ao", "d", "r", store),
                      observer=Observer(_cfg(), state_store=store),
                      adapter=adapter, gate=IntegrationGate(store),
                      store=store, verifier=FakeVerifierProvider())
    loop._transition(ProjectState.WORKER_RUNNING, "test", "setup", {})
    loop._transition(ProjectState.GATE_PENDING, "test", "setup", {})
    return loop, store, wtdir


def test_verifier_git_failure_goes_human_not_crash(tmp_path, monkeypatch):
    loop, store, wtdir = _make_loop(tmp_path, monkeypatch)
    # inject the git failure the reviewer demonstrated
    monkeypatch.setattr(wt, "changed_paths", lambda *a, **k: None)
    loop._transition(ProjectState.VERIFIER_PENDING, "test", "setup", {})
    loop._run_verifier()                    # must NOT raise TypeError
    assert loop.state == ProjectState.HUMAN


def test_commit_all_excludes_pyc_artifacts(tmp_path):
    repo = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo),
                   check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo),
                   check=True)
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo),
                   check=True)
    # worker-run noise appears in the working tree
    cache = repo / "__pycache__"
    cache.mkdir()
    (cache / "app.cpython-312.pyc").write_bytes(b"\x00\x01\x02")
    (repo / "app.py").write_text("x = 2\n")
    head = wt.commit_all(str(repo), "sidecar commit")
    assert head
    tracked = subprocess.run(["git", "ls-files"], cwd=str(repo),
                             capture_output=True, text=True).stdout
    assert "app.py" in tracked
    assert ".pyc" not in tracked
    assert "__pycache__" not in tracked


def test_freeze_base_writes_info_exclude(tmp_path):
    repo = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo),
                   check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo),
                   check=True)
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo),
                   check=True)
    store = StateStore(str(tmp_path / "cl.db"))
    base = wt.freeze_base(str(repo), store, "TASK-X", scope="w")
    assert base
    excl = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "__pycache__/" in excl and "*.pyc" in excl
    # and git honors it: an untracked .pyc must NOT be addable via -A
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "a.pyc").write_bytes(b"\x00")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                            cwd=str(repo), capture_output=True,
                            text=True).stdout
    assert ".pyc" not in staged


def test_id_helpers_are_clean():
    mid = make_id("VERIFY")
    assert re.fullmatch(r"VERIFY-\d{8}T\d{12,}-[0-9a-f]{6}", mid), mid
    assert make_id("VERIFY") != make_id("VERIFY")  # random suffix
    sid = stable_id("L0", '{"code":-32602,"message":"x"}', length=12)
    assert re.fullmatch(r"L0-[0-9a-f]{12}", sid), sid
    # deterministic: same content -> same id (dedup semantics preserved)
    assert sid == stable_id("L0", '{"code":-32602,"message":"x"}',
                            length=12)
    a1 = stable_id("ACTION", "AUDIT-abc", length=16)
    assert re.fullmatch(r"ACTION-[0-9a-f]{16}", a1), a1
