"""Multi-worker worktree plumbing tests (temp git repos, no network/AO).

Covers the two properties that make N parallel workers safe:
1. per-scope base isolation — two workers on the same task freeze against
   their OWN worktree HEADs; one worker's edits never leak into the other's
   path-gate/diff;
2. sidecar commit + integration merge — clean merge of disjoint edits,
   deterministic conflict detection.
"""
import subprocess
from pathlib import Path

import pytest

from loopcore import worktree as wt
from loopcore.state_store import StateStore


def _run(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run(path, "init", "-q")
    _run(path, "config", "user.name", "t")
    _run(path, "config", "user.email", "t@t")
    (path / "app.py").write_text("x = 1\n", encoding="utf-8")
    _run(path, "add", "-A")
    _run(path, "commit", "-q", "-m", "init")
    return path


def _add_worktree(repo: Path, name: str) -> Path:
    out = Path(str(repo) + "-" + name)
    _run(repo, "worktree", "add", "-q", "-b", name, str(out))
    return out


def test_two_workers_freeze_isolated_bases(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    w1 = _add_worktree(repo, "w1")
    w2 = _add_worktree(repo, "w2")
    # worker 1 commits an edit; worker 2 stays at base
    (w1 / "app.py").write_text("x = 2\n", encoding="utf-8")
    _run(w1, "commit", "-q", "-am", "w1 edit")
    store = StateStore(tmp_path / "s.db")
    b1 = wt.freeze_base(w1, store, "TASK-1", scope="worker-1")
    b2 = wt.freeze_base(w2, store, "TASK-1", scope="worker-2")
    # each froze ITS OWN head (w1 froze after its commit -> its changes are
    # INVISIBLE to its own gate; w2 froze the original). Different scopes ->
    # different sidecars, no cross-talk.
    assert b1 and b2
    head1 = wt._current_head(w1)
    head2 = wt._current_head(w2)
    assert b1 == head1 and b2 == head2
    # w2's changed paths vs ITS base: nothing
    assert wt.changed_paths(w2, b2) == []
    # w1 vs its base: nothing either (its edit predates its freeze) — the
    # point is neither sees the OTHER's tree
    assert wt.changed_paths(w1, b1) == []


def test_path_gate_scoped_per_worker(tmp_path):
    """One worker edits tests/ — only THAT worker's path gate trips."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "tests").mkdir()
    (repo / "tests" / "t.py").write_text("def test_x(): pass\n",
                                         encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "tests")
    w_good = _add_worktree(repo, "good")
    w_bad = _add_worktree(repo, "bad")
    (w_good / "app.py").write_text("x = 2\n", encoding="utf-8")
    (w_bad / "tests" / "t.py").write_text("def test_x(): assert False\n",
                                          encoding="utf-8")
    store = StateStore(tmp_path / "s.db")
    bg = wt.freeze_base(w_good, store, "TASK-9", scope="good")
    bb = wt.freeze_base(w_bad, store, "TASK-9", scope="bad")
    forbidden, outside = wt.path_violations(
        w_good, bg, allowed_paths=["app.py"], forbidden_paths=["tests/**"])
    assert forbidden == [] and outside == []
    forbidden2, outside2 = wt.path_violations(
        w_bad, bb, allowed_paths=["app.py"], forbidden_paths=["tests/**"])
    assert forbidden2 == ["tests/t.py"]


def test_commit_all_and_clean_merge(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    w1 = _add_worktree(repo, "w1")
    w2 = _add_worktree(repo, "w2")
    # disjoint edits: w1 touches app.py, w2 creates new file
    (w1 / "app.py").write_text("x = 2\n", encoding="utf-8")
    (w2 / "newmod.py").write_text("y = 3\n", encoding="utf-8")
    store = StateStore(tmp_path / "s.db")
    wt.freeze_base(w1, store, "T", scope="w1")
    wt.freeze_base(w2, store, "T", scope="w2")
    integ = tmp_path / "integration"
    assert wt.add_integration_worktree(str(repo), "integration", str(integ))
    sha1 = wt.commit_all(w1, "subtask w1")
    sha2 = wt.commit_all(w2, "subtask w2")
    assert sha1 and sha2
    r1 = wt.merge_worktree(str(integ), str(w1))
    assert r1.status == wt.MergeOutcome.OK, r1.detail
    r2 = wt.merge_worktree(str(integ), str(w2))
    assert r2.status == wt.MergeOutcome.OK, r2.detail
    # both edits present on the integration branch
    assert (integ / "app.py").read_text(encoding="utf-8") == "x = 2\n"
    assert (integ / "newmod.py").read_text(encoding="utf-8") == "y = 3\n"


def test_merge_conflict_detected(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    w1 = _add_worktree(repo, "w1")
    w2 = _add_worktree(repo, "w2")
    (w1 / "app.py").write_text("x = 100\n", encoding="utf-8")
    (w2 / "app.py").write_text("x = 200\n", encoding="utf-8")
    wt.commit_all(w1, "c1")
    integ = tmp_path / "integration"
    wt.add_integration_worktree(str(repo), "integration", str(integ))
    r1 = wt.merge_worktree(str(integ), str(w1))
    assert r1.status == wt.MergeOutcome.OK
    wt.commit_all(w2, "c2")
    r2 = wt.merge_worktree(str(integ), str(w2))
    assert r2.status == wt.MergeOutcome.CONFLICT
    # merge was aborted: tree still has w1's value, no conflict markers
    assert (integ / "app.py").read_text(encoding="utf-8") == "x = 100\n"
    assert "<<<<<<<" not in (integ / "app.py").read_text(encoding="utf-8")


def test_commit_all_no_changes_returns_head(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    head = wt._current_head(repo)
    assert wt.commit_all(repo, "noop") == head


def test_commit_all_materializes_source_beside_ignored_cache(tmp_path):
    """Real regression for MISSION-PANEL-20260901-200228."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q")
    _run(repo, "config", "user.name", "t")
    _run(repo, "config", "user.email", "t@t")
    (repo / ".gitignore").write_text(
        "**pycache**/\n*.pyc\n.pytest_cache/\n", encoding="utf-8")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _run(repo, "add", "--", ".gitignore", "app.py")
    _run(repo, "commit", "-q", "-m", "baseline")
    baseline = wt._current_head(repo)

    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    cache = repo / "__pycache__" / "app.cpython-312.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"\x00\x01\x02")
    assert _run(repo, "check-ignore", str(cache.relative_to(repo))).returncode == 0

    head = wt.commit_all(repo, "subtask S1")

    assert head != baseline
    committed = _run(repo, "diff-tree", "--no-commit-id", "--name-only",
                     "-r", head).stdout.splitlines()
    assert committed == ["app.py"]
    assert _run(repo, "show", "HEAD:app.py").stdout == "x = 2\n"
    assert _run(repo, "status", "--short").stdout == ""
    assert cache.is_file()


def test_commit_all_materializes_untracked_source(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "new_module.py").write_text("value = 2\n", encoding="utf-8")

    head = wt.commit_all(repo, "add source")

    assert "new_module.py" in _run(repo, "ls-tree", "--name-only",
                                    head).stdout.splitlines()
    assert _run(repo, "status", "--short").stdout == ""


def test_commit_all_materializes_deleted_source(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    deleted = repo / "obsolete.py"
    deleted.write_text("obsolete = True\n", encoding="utf-8")
    _run(repo, "add", "--", "obsolete.py")
    _run(repo, "commit", "-q", "-m", "add obsolete source")
    baseline = wt._current_head(repo)
    deleted.unlink()

    head = wt.commit_all(repo, "delete source")

    assert head != baseline
    assert "obsolete.py" not in _run(repo, "ls-tree", "--name-only",
                                      head).stdout.splitlines()
    assert _run(repo, "diff-tree", "--no-commit-id", "--name-status",
                "-r", head).stdout.strip() == "D\tobsolete.py"


def test_commit_all_materializes_already_staged_source(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    baseline = wt._current_head(repo)
    (repo / "app.py").write_text("x = 3\n", encoding="utf-8")
    _run(repo, "add", "--", "app.py")

    head = wt.commit_all(repo, "staged source")

    assert head != baseline
    assert _run(repo, "show", "HEAD:app.py").stdout == "x = 3\n"
    assert _run(repo, "status", "--short").stdout == ""


def test_commit_all_does_not_force_user_ignored_file(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    _run(repo, "add", "--", ".gitignore")
    _run(repo, "commit", "-q", "-m", "ignore policy")
    (repo / ".env").write_text("LOCAL_ONLY=1\n", encoding="utf-8")
    (repo / "app.py").write_text("x = 4\n", encoding="utf-8")

    head = wt.commit_all(repo, "respect ignore policy")

    tracked = _run(repo, "ls-tree", "--name-only", head).stdout.splitlines()
    assert ".env" not in tracked
    assert _run(repo, "check-ignore", ".env").returncode == 0
    assert (repo / ".env").is_file()
    assert _run(repo, "status", "--short").stdout == ""


def test_commit_all_uses_literal_pathspec_for_space_in_path(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    source = repo / "module with space.py"
    source.write_text("value = 5\n", encoding="utf-8")

    head = wt.commit_all(repo, "space path")

    assert "module with space.py" in _run(
        repo, "ls-tree", "--name-only", head).stdout.splitlines()
    assert _run(repo, "show", "HEAD:module with space.py").stdout == \
        "value = 5\n"


def _mock_materialization(monkeypatch, add_results, commit_results,
                          final_head="NEW"):
    calls = []
    heads = iter(["BASE", final_head])
    adds = iter(add_results)
    commits = iter(commit_results)

    monkeypatch.setattr(wt, "_current_head", lambda _path: next(heads))
    monkeypatch.setattr(wt, "changed_paths", lambda *_args: ["app.py"])
    monkeypatch.setattr(wt.time, "sleep", lambda _seconds: None)

    def git_check(_path, *args, **_kwargs):
        calls.append(args)
        if args[0] == "add":
            return next(adds)
        if args[0] == "commit":
            return next(commits)
        raise AssertionError("unexpected git command: %r" % (args,))

    monkeypatch.setattr(wt, "_git_check", git_check)
    return calls


def test_commit_all_retries_commit_once_then_succeeds(monkeypatch):
    calls = _mock_materialization(
        monkeypatch,
        add_results=[(True, ""), (True, "")],
        commit_results=[(False, "transient stderr"), (True, "")])

    assert wt.commit_all("worker", "subtask S1") == "NEW"
    assert [args[0] for args in calls].count("commit") == 2
    assert [args[0] for args in calls] == ["add", "commit", "add", "commit"]


def test_commit_all_raises_after_two_commit_failures(monkeypatch):
    calls = _mock_materialization(
        monkeypatch,
        add_results=[(True, ""), (True, "")],
        commit_results=[(False, "first fake stderr"),
                        (False, "last fake stderr")])

    with pytest.raises(RuntimeError) as raised:
        wt.commit_all("worker", "subtask S1")

    assert "git commit failed" in str(raised.value)
    assert "last fake stderr" in str(raised.value)
    assert [args[0] for args in calls].count("commit") == 2


def test_commit_all_recovers_from_first_add_failure(monkeypatch):
    calls = _mock_materialization(
        monkeypatch,
        add_results=[(False, "transient add stderr"), (True, "")],
        commit_results=[(True, "")])

    assert wt.commit_all("worker", "subtask S1") == "NEW"
    assert [args[0] for args in calls] == ["add", "add", "commit"]


def test_commit_all_raises_after_two_add_failures(monkeypatch):
    calls = _mock_materialization(
        monkeypatch,
        add_results=[(False, "first add stderr"),
                     (False, "last add stderr")],
        commit_results=[])

    with pytest.raises(RuntimeError) as raised:
        wt.commit_all("worker", "subtask S1")

    assert "git add failed" in str(raised.value)
    assert "last add stderr" in str(raised.value)
    assert [args[0] for args in calls] == ["add", "add"]


def test_commit_all_uses_only_add_and_policy_respecting_commit(monkeypatch):
    calls = _mock_materialization(
        monkeypatch,
        add_results=[(True, "")],
        commit_results=[(True, "")])

    assert wt.commit_all("worker", "subtask S1") == "NEW"
    flattened = [token for args in calls for token in args]
    assert [args[0] for args in calls] == ["add", "commit"]
    assert calls[0] == ("add", "-A", "--", ":(literal)app.py")
    assert "-f" not in flattened
    assert "--no-verify" not in flattened
    assert not {"config", "reset", "restore", "checkout"} & set(flattened)


def test_commit_all_inspection_failure_is_explicit(monkeypatch):
    monkeypatch.setattr(wt, "_current_head", lambda _path: None)

    with pytest.raises(RuntimeError, match="git inspection failed"):
        wt.commit_all("worker", "subtask S1")


def test_commit_all_change_inspection_failure_is_explicit(monkeypatch):
    monkeypatch.setattr(wt, "_current_head", lambda _path: "BASE")
    monkeypatch.setattr(wt, "changed_paths", lambda *_args: None)

    with pytest.raises(RuntimeError,
                       match="git inspection failed: unable to inspect"):
        wt.commit_all("worker", "subtask S1")


def test_commit_all_requires_head_after_success(monkeypatch):
    calls = _mock_materialization(
        monkeypatch,
        add_results=[(True, "")],
        commit_results=[(True, "")],
        final_head=None)

    with pytest.raises(RuntimeError,
                       match="git commit succeeded but reading HEAD failed"):
        wt.commit_all("worker", "subtask S1")
    assert [args[0] for args in calls] == ["add", "commit"]


def test_git_diff_text_excludes_committed_pyc(tmp_path):
    """A worker's `git add -A` commit sweeps __pycache__/*.pyc into the repo;
    the Verifier-facing diff must not show them (MISSION-QUICK-012 S1 was
    FAILed three times over this noise despite correct source delivery)."""
    repo = _init_repo(tmp_path)
    base = wt._current_head(str(repo))
    (repo / "app.py").write_text("x = 1\ndef square(a):\n    return a * a\n",
                                 encoding="utf-8")
    pc = repo / "__pycache__"
    pc.mkdir()
    (pc / "app.cpython-312.pyc").write_bytes(b"\x00\x01\x02")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "work")
    diff = wt.git_diff_text(str(repo), base)
    assert "def square" in diff          # real change visible
    assert "__pycache__" not in diff      # artifact noise gone
    assert ".pyc" not in diff
    # changed_paths stays clean as well
    assert wt.changed_paths(str(repo), base) == ["app.py"]
