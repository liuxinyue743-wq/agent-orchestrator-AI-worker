"""Regression (review 簇二): the NO_PROGRESS rule in 'strong' mode requires
strong-progress events, but the production loop had NO strong-progress
emitter — any active, healthy worker would eventually be flagged NO_PROGRESS.

Fix: (a) default config runs progress_mode=weak (file edits count; thrash is
caught by diff-fingerprint alerts), and (b) a green integration gate now
feeds a strong-progress event into the observer, closing the NO_PROGRESS
clock for the task.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import yaml

from loopcore.action_executor import ActionExecutor
from loopcore.auditor import FakeAuditorProvider
from loopcore.closed_loop import ClosedLoop
from loopcore.event_observer import Observer
from loopcore.mission_contracts import ProjectState, TaskSpec
from loopcore.mission_gate import IntegrationGate
from loopcore.planner_adapter import FakePlannerProvider
from loopcore.state_store import StateStore
from loopcore.verifier import FakeVerifierProvider
from tests.sidecar_port.test_phase3 import _cfg
from tests.sidecar_port.test_contracts import _task_spec


def _make_loop(tmp_path, monkeypatch):
    store = StateStore(str(tmp_path / "cl.db"))
    task = TaskSpec.from_dict(_task_spec())
    task.worker_session_id = "w-strong"
    task.gate_commands = ["python -c \"pass\""]
    wt = tmp_path / "worktrees" / task.project_id / task.worker_session_id
    wt.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(wt), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(wt),
                   check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(wt),
                   check=True)
    (wt / "app.py").write_text("def divide(a, b):\n    return a / b\n")
    subprocess.run(["git", "add", "-A"], cwd=str(wt), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(wt), check=True)
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path))
    adapter = MagicMock()
    adapter.get_worker_status.return_value = {"id": task.worker_session_id,
                                              "status": "idle"}
    ex = ActionExecutor("ao", "d", "r", store)
    obs = Observer(_cfg(), state_store=store)
    loop = ClosedLoop(task=task, cfg=_cfg(), auditor=FakeAuditorProvider(),
                      planner=FakePlannerProvider(), executor=ex,
                      observer=obs, adapter=adapter,
                      gate=IntegrationGate(store), store=store,
                      verifier=FakeVerifierProvider())
    loop._transition(ProjectState.WORKER_RUNNING, "test", "setup", {})
    loop._transition(ProjectState.GATE_PENDING, "test", "setup", {})
    return loop, obs


def test_gate_pass_feeds_strong_progress_event(tmp_path, monkeypatch):
    loop, obs = _make_loop(tmp_path, monkeypatch)
    spy = MagicMock(wraps=obs.feed)
    loop.observer.feed = spy
    loop._run_gate()
    strong = [c.args[0] for c in spy.call_args_list
              if getattr(c.args[0], "progress_strength", None) == "strong"]
    assert strong, "gate pass must emit a strong-progress event"
    ev = strong[0]
    assert ev.task_id == loop.task.task_id
    assert ev.progress is True and ev.activity is True


def test_default_config_uses_weak_progress_mode():
    from pathlib import Path
    cfg = yaml.safe_load(Path("config/default.yaml").read_text(
        encoding="utf-8"))
    mode = cfg["thresholds"]["no_progress"]["progress_mode"]
    assert mode == "weak", \
        "default must be weak until every strong-progress emitter exists"
