"""Cluster-1 regressions (review: Verifier evidence was structurally wrong):

1. The integration worktree must branch from the MAIN repo HEAD, never from
   the first-finished worker's HEAD (MISSION-QUICK-010 phantom 'square
   missing' root cause).
2. The final mission gate separates pre-existing (baseline) failures from
   mission-caused ones: legacy-only red -> MISSION_DONE possible; NEW
   failures -> fatal.
3. extract_failure_ids parses both pytest -q section headers and -rf lines.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from loopcore import worktree as wt
from loopcore.codex_cli import CodexCliError
from loopcore.mission_gate import IntegrationGate
from loopcore.test_failures import extract_failure_ids
from tests.sidecar_port.test_mission import _mc
from loopcore.mission_contracts import ProjectState
from loopcore.verifier import FakeVerifierProvider


def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _repo_with_worker(tmp_path):
    """Main repo with one commit + a worker worktree carrying an EXTRA commit."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    _git(main, "config", "user.name", "t")
    _git(main, "config", "user.email", "t@t")
    (main / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "main HEAD")
    main_head = _git(main, "rev-parse", "HEAD").stdout.strip()
    worker = tmp_path / "worker-1"
    _git(main, "worktree", "add", "-q", "-b", "w1", str(worker))
    (worker / "w.py").write_text("y = 2\n", encoding="utf-8")
    _git(worker, "add", "-A")
    _git(worker, "commit", "-q", "-m", "worker commit")
    return main, worker, main_head


def test_integration_worktree_branches_from_main_head(tmp_path):
    main, worker, main_head = _repo_with_worker(tmp_path)
    target = tmp_path / "integ"
    out = wt.add_integration_worktree(str(worker), "integration-T1",
                                      str(target))
    assert out == str(target)
    integ_head = _git(target, "rev-parse", "HEAD").stdout.strip()
    assert integ_head == main_head
    # the worker's unmerged commit must NOT be in the integration base
    assert not (target / "w.py").exists()


def test_extract_failure_ids():
    out_q = "..FF..\n===== FAILURES =====\n_____ test_divide_normal _____\n" \
            "_____ test_divide_by_zero _____\n"
    assert extract_failure_ids(out_q) == ["test_divide_by_zero",
                                          "test_divide_normal"]
    out_rf = "FAILED tests/test_a.py::test_x - assert 1 == 2\n" \
             "ERROR tests/test_b.py::test_y\n"
    ids = extract_failure_ids(out_rf)
    assert "tests/test_a.py::test_x" in ids
    assert "tests/test_b.py::test_y" in ids
    assert extract_failure_ids("6 passed in 0.01s\n") == []


def _seed_done_mission(tmp_path):
    """A mission with both subtasks DONE and merged (reuses test_mission)."""
    data_dir = tmp_path / "ao-data"
    proj = data_dir / "worktrees" / "closed-loop-demo"
    proj.mkdir(parents=True)
    _git(proj, "init", "-q")
    _git(proj, "config", "user.name", "t")
    _git(proj, "config", "user.email", "t@t")
    (proj / "app.py").write_text("x=1\n", encoding="utf-8")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-q", "-m", "init")
    workers = {}
    for name in ("sess-S1", "sess-S2"):
        _git(proj, "worktree", "add", "-q", "-b", name,
             str(proj / name))
        workers[name] = proj / name
    (proj / "sess-S1" / "app.py").write_text(
        "def divide(a,b):\n    if b==0: raise ValueError\n    return a/b\n",
        encoding="utf-8")
    (proj / "sess-S2" / "math2.py").write_text(
        "def multiply(a,b): return a*b\n", encoding="utf-8")
    mc, store = _mc(tmp_path)
    mc.adapter.get_session_workspace.side_effect = (
        lambda session_id: str(workers[session_id]))
    mc.step()  # decompose

    def fake_spawn(task):
        return "sess-" + task.task_id[-2:]
    with patch.object(mc.executor, "spawn_initial_worker",
                      side_effect=fake_spawn):
        mc.step()  # dispatch S1
    s1 = [s for s in mc.plan.subtasks if not s.dependencies][0].subtask_id
    s2 = [s for s in mc.plan.subtasks if s.dependencies][0].subtask_id
    store.record_transition(task_id=s1, from_state="WORKER_RUNNING",
                            to_state="DONE", actor="t", reason="t",
                            evidence={})
    with patch.object(mc.executor, "spawn_initial_worker",
                      side_effect=fake_spawn):
        mc.step()  # dispatch S2 + merge S1
    store.record_transition(task_id=s2, from_state="WORKER_RUNNING",
                            to_state="DONE", actor="t", reason="t",
                            evidence={})
    return mc, store, data_dir


def test_final_gate_tolerates_legacy_baseline_failures(tmp_path):
    mc, store, data_dir = _seed_done_mission(tmp_path)
    # baseline captured a legacy red test on the pristine tree
    sidecar = Path(str(store.path) + ".baseline-%s.json"
                   % mc.mission.mission_id)
    sidecar.write_text(json.dumps({"failures": ["test_legacy"]}),
                       encoding="utf-8")
    gate_red_legacy = MagicMock(ok=False, results=[{
        "command": "pytest", "stdout": "..F..\n_____ test_legacy _____\n",
        "stderr": ""}])
    with patch.object(IntegrationGate, "run",
                      return_value=gate_red_legacy) as gate_run:
        r = mc.step()
    assert r["state"] == "MISSION_DONE"
    assert gate_run.call_args.kwargs["require_clean"] is True
    # the tolerance is recorded in the mission reason
    assert "legacy" in mc._read_state().get("reason", "")


def test_final_gate_new_failure_is_fatal(tmp_path):
    mc, store, data_dir = _seed_done_mission(tmp_path)
    sidecar = Path(str(store.path) + ".baseline-%s.json"
                   % mc.mission.mission_id)
    sidecar.write_text(json.dumps({"failures": ["test_legacy"]}),
                       encoding="utf-8")
    gate_red_new = MagicMock(ok=False, results=[{
        "command": "pytest",
        "stdout": "..FF..\n_____ test_legacy _____\n_____ test_new_break _____\n",
        "stderr": ""}])
    with patch.object(IntegrationGate, "run", return_value=gate_red_new):
        r = mc.step()
    assert r["state"] == "HUMAN"


def test_final_gate_initial_dirty_is_human_without_verifier(tmp_path):
    mc, store, _data_dir = _seed_done_mission(tmp_path)
    integration = Path(store.path).parent / "integration"
    (integration / "unexpected.txt").write_text("dirty", encoding="utf-8")
    verifier = MagicMock(wraps=FakeVerifierProvider())
    mc.verifier = verifier

    result = mc.step()

    assert result["state"] == "HUMAN"
    assert "initial repository not clean" in mc._read_state()["reason"]
    verifier.verify.assert_not_called()


def test_final_gate_exit_zero_mutation_is_human_without_verifier(tmp_path):
    mc, store, _data_dir = _seed_done_mission(tmp_path)
    mc.mission.gate_commands = [
        '"%s" -c "from pathlib import Path; '
        "Path('gate-mutated.txt').write_text('changed')\"" % sys.executable]
    verifier = MagicMock(wraps=FakeVerifierProvider())
    mc.verifier = verifier

    result = mc.step()

    assert result["state"] == "HUMAN"
    assert "Gate changed repository state" in mc._read_state()["reason"]
    verifier.verify.assert_not_called()
    assert (Path(store.path).parent / "integration" /
            "gate-mutated.txt").exists()


def test_final_gate_probe_failure_is_human_without_verifier(tmp_path):
    mc, _store, _data_dir = _seed_done_mission(tmp_path)
    verifier = MagicMock(wraps=FakeVerifierProvider())
    mc.verifier = verifier

    with patch(
            "loopcore.mission_gate.wt.git_state_snapshot",
            side_effect=wt.GitStateSnapshotError("HEAD unavailable")):
        result = mc.step()

    assert result["state"] == "HUMAN"
    assert "pre-Gate Git probe failed" in mc._read_state()["reason"]
    verifier.verify.assert_not_called()


def test_final_verifier_transport_failure_retries_next_tick(tmp_path):
    """A merged mission is not rejected for one verifier transport outage."""
    mc, store, data_dir = _seed_done_mission(tmp_path)

    class FlakyVerifier:
        calls = 0

        def verify(self, inp, verify_id):
            self.calls += 1
            if self.calls == 1:
                raise CodexCliError("final verifier timed out")
            return FakeVerifierProvider().verify(inp, verify_id)

    flaky = FlakyVerifier()
    mc.verifier = flaky
    gate_pass = MagicMock(ok=True, results=[{
        "command": "python -m pytest -q", "stdout": "2 passed\n",
        "stderr": ""}])

    with patch.object(IntegrationGate, "run", return_value=gate_pass):
        first = mc.step()
        integration = Path(store.path).parent / "integration"
        head_after_merge = _git(integration, "rev-parse", "HEAD").stdout.strip()
        second = mc.step()

    assert first["acted"] is False
    assert "CodexCliError: final verifier timed out" in first["error"]
    assert first["state"] not in ("MISSION_DONE", "HUMAN", "FAILED")
    assert integration.is_dir()
    assert len(mc.merged) == len(mc.tasks)
    assert store._conn.execute(
        "SELECT count(*) FROM verifications").fetchone()[0] == 1
    assert second["state"] == "MISSION_DONE"
    assert flaky.calls == 2
    assert mc._loop_error_streak == 0
    assert _git(integration, "rev-parse", "HEAD").stdout.strip() == \
        head_after_merge
