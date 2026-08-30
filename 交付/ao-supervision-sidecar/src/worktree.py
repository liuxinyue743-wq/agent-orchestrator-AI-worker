"""Deterministic worktree introspection for the closed loop.

A plain PROGRAM (not an agent): no model, no AO control, only read-only git
inspection inside the worker's worktree. Used by the Integration Gate (path
gating) and the closed-loop controller (progress fingerprinting / thrash
detection).

All changed-path detection is relative to a frozen base commit so that
staged / committed / untracked / renamed / deleted files are all seen
(previously `git diff --name-only` alone missed untracked files and files
the worker `git add`-ed or reverted).

  base commit      : first resolved HEAD when freeze() is called (or the
                     last committed SHA if a previous run already froze it).
  changed paths    : git diff --name-status <base>...HEAD  PLUS
                     git ls-files --others --exclude-standard  PLUS
                     git diff --name-status <base>  (working tree).
  diff fingerprint : sha1 of (sorted changed paths + HEAD) — stable across
                     re-reads, changes when the worker makes/undoes edits.
"""
from __future__ import annotations

import fnmatch
import hashlib
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple


def _git(worktree: str, *args: str, timeout: int = 30) -> str:
    try:
        proc = subprocess.run(["git", "-C", worktree, *args],
                              capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
        return proc.stdout or ""
    except Exception:
        return ""


def _current_head(worktree: str) -> Optional[str]:
    out = _git(worktree, "rev-parse", "HEAD").strip()
    return out or None


def freeze_base(worktree: str, store, task_id: str, scope: str = "") -> str:
    """Return the base commit for this task, freezing HEAD on first call.

    The SHA is kept in a JSON sidecar OUTSIDE the worktree (worker edits cannot
    tamper with it); a marker counter in the store signals "already frozen" so a
    later call returns the frozen SHA instead of re-reading HEAD (the worker may
    have committed since, which would hide its edits from the gate).

    `scope` isolates concurrent workers on the SAME task: the counter key
    becomes base_commit:<task>:<scope> and the sidecar file is named per
    scope. Without it, two parallel workers would share one frozen SHA and
    each diff/path-gate would be computed against the WRONG worktree's HEAD.
    """
    tag = ("%s:%s" % (task_id, scope)) if scope else task_id
    key = "base_commit:" + tag
    existing = store.counter_get(key)
    if existing:
        return _read_base_sidecar(worktree, tag)
    head = _current_head(worktree)
    if not head:
        return ""
    store.counter_set(key, 1)
    _write_base_sidecar(worktree, tag, head)
    return head


def _sidecar_path(worktree: str, tag: str) -> Path:
    # lives outside the worktree so worker edits cannot tamper with it
    return Path(worktree).parent / (".base-" + tag.replace(":", "-") + ".json")


def _read_base_sidecar(worktree: str, tag: str) -> str:
    import json
    p = _sidecar_path(worktree, tag)
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("base_commit", "")
    except Exception:
        return ""


def _write_base_sidecar(worktree: str, tag: str, sha: str) -> None:
    import json
    p = _sidecar_path(worktree, tag)
    p.write_text(json.dumps({"base_commit": sha}), encoding="utf-8")


def changed_paths(worktree: str, base_commit: str) -> List[str]:
    """Full changed-path set relative to base_commit.

    Covers: modified/added/deleted/renamed (tracked) + untracked new files.
    """
    paths: set = set()
    # committed changes vs base
    if base_commit:
        out = _git(worktree, "diff", "--name-status", base_commit + "...HEAD")
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                paths.add(parts[-1].strip())
                # renames: "R100\told\tnew"
                if parts[0].startswith("R") and len(parts) >= 3:
                    paths.add(parts[-1].strip())
    # uncommitted working-tree changes vs base (staged + unstaged)
    if base_commit:
        out = _git(worktree, "diff", "--name-status", base_commit)
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                paths.add(parts[-1].strip())
                if parts[0].startswith("R") and len(parts) >= 3:
                    paths.add(parts[-1].strip())
    # untracked files (worker created new files)
    out = _git(worktree, "ls-files", "--others", "--exclude-standard")
    for line in out.splitlines():
        line = line.strip()
        if line:
            paths.add(line)
    return sorted(p for p in paths if p and not _is_artifact(p))


_ARTIFACT_MARKERS = (
    "__pycache__", ".pyc", ".pyo", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".coverage", ".tox", ".hypothesis", ".eggs",
)


def _is_artifact(path: str) -> bool:
    """True if `path` is a test/build artifact, not a source edit.

    The gate runs `pytest` in the worktree, which generates __pycache__ and
    .pytest_cache regardless of what the worker edited. Flagging those as
    'modified a forbidden/outside path' was a false positive (the old gate
    halted a passing run on `tests/__pycache__/*.pyc` -> HUMAN).
    """
    p = path.replace("\\", "/")
    return any(marker in p for marker in _ARTIFACT_MARKERS)


def diff_fingerprint(worktree: str, base_commit: str) -> str:
    """Stable fingerprint of the current change set + HEAD.

    Changes when the worker edits, commits, or reverts any file; stable on
    repeated reads of an unchanged tree. Used to detect thrash (same diff
    reappearing / being undone).
    """
    paths = changed_paths(worktree, base_commit)
    head = _current_head(worktree) or ""
    raw = head + "\n" + "\n".join(paths)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def path_violations(worktree: str, base_commit: str, *,
                    allowed_paths: List[str],
                    forbidden_paths: List[str]) -> Tuple[List[str], List[str]]:
    """Return (forbidden_violations, allowed_violations).

    forbidden_violations: changed paths matching any forbidden pattern.
    allowed_violations : changed paths OUTSIDE every allowed pattern
                         (empty allowed_paths means "no restriction").
    """
    changed = changed_paths(worktree, base_commit)
    forbidden = []
    for path in changed:
        for pat in forbidden_paths or []:
            p = pat.replace("\\", "/").rstrip("/")
            if fnmatch.fnmatch(path, p) or fnmatch.fnmatch(path, p + "/*"):
                forbidden.append(path)
                break
    allowed = []
    if allowed_paths:
        for path in changed:
            if path in forbidden:
                continue
            if not any(
                fnmatch.fnmatch(path, a.replace("\\", "/").rstrip("/")) or
                fnmatch.fnmatch(path, a.replace("\\", "/").rstrip("/") + "/*")
                for a in allowed_paths):
                allowed.append(path)
    return forbidden, allowed


def git_diff_text(worktree: str, base_commit: str, limit: int = 4000) -> str:
    """Diff text for the Auditor/Verifier, relative to base_commit.

    Must cover EVERYTHING the worker changed since the base:
      - the committed range base..HEAD (workers often `git commit` mid-task,
        which a plain working-tree diff would hide), and
      - untracked NEW files (git diff does not show them at all) — staged
        into the index just for the diff, then unstaged.
    """
    if not base_commit:
        return _git(worktree, "diff")[:limit]
    out = _git(worktree, "diff", base_commit + "...HEAD")
    out += "\n" + _git(worktree, "diff", base_commit)
    untracked = [p for p in _git(worktree, "ls-files", "--others",
                                 "--exclude-standard").splitlines()
                 if p and not _is_artifact(p)]
    if untracked:
        _git(worktree, "add", "-N", "--", *untracked)
        out += "\n" + _git(worktree, "diff", "--", *untracked)
        _git(worktree, "reset", "-q", "--", *untracked)
    return out[:limit]


# ------------------------------------------------------ integration merge
def _git_check(worktree: str, *args: str, timeout: int = 60) -> Tuple[bool, str]:
    """Run git and require success; returns (ok, combined output)."""
    try:
        proc = subprocess.run(["git", "-C", worktree, *args],
                              capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return proc.returncode == 0, out
    except Exception as e:  # noqa
        return False, str(e)


def commit_all(worktree: str, message: str) -> Optional[str]:
    """Sidecar-side commit of everything in the worker's worktree.

    Called ONLY by trusted controller code (never the worker) so the merge
    pipeline has a clean commit to fetch. Returns the new HEAD, or None when
    there was nothing to commit / git failed.
    """
    # exclude artifacts from the commit (they'd collide across worktrees)
    changed = changed_paths(worktree, _current_head(worktree) or "")
    if not changed:
        return _current_head(worktree)
    ok, _ = _git_check(worktree, "add", "-A", "--", ".")
    if not ok:
        return None
    ok, _ = _git_check(worktree, "commit", "-q", "-m", message)
    if not ok:
        return None
    return _current_head(worktree)


def add_integration_worktree(repo_path: str, branch: str,
                             target_path: str) -> Optional[str]:
    """Create (or reuse) an integration worktree for a mission.

    `repo_path` is any worktree/clone of the project (we only need its git
    dir); the integration worktree is checked out at `branch`, created from
    the current HEAD if it does not exist yet.
    """
    Path(target_path).mkdir(parents=True, exist_ok=True)
    ok, out = _git_check(repo_path, "worktree", "add", "--checkout",
                         "-B", branch, target_path)
    if ok:
        return target_path
    # "already exists" variants: try plain add (branch exists), then reuse
    ok, _ = _git_check(repo_path, "worktree", "add", target_path, branch)
    if ok:
        return target_path
    # already registered AND directory present -> reuse as-is
    if Path(target_path).exists() and (Path(target_path) / ".git").exists():
        return target_path
    return None


class MergeOutcome:
    """Result of merging one worker worktree into the integration worktree."""
    OK = "ok"
    CONFLICT = "conflict"
    ERROR = "error"

    def __init__(self, status: str, detail: str = ""):
        self.status = status
        self.detail = detail

    def __repr__(self):
        return "MergeOutcome(%s, %r)" % (self.status, self.detail[:120])


def merge_worktree(integration_wt: str, source_wt: str) -> MergeOutcome:
    """Merge a finished worker's worktree HEAD into the integration worktree.

    Works for linked worktrees and independent clones alike: fetch from the
    source PATH (a local path is a valid git remote URL), then merge
    FETCH_HEAD. Conflicts are detected deterministically and reported — the
    controller routes them back to the Planner (bounded by mission budgets).
    """
    ok, out = _git_check(integration_wt, "fetch", "--quiet", source_wt, "HEAD")
    if not ok:
        return MergeOutcome(MergeOutcome.ERROR, "fetch: " + out[:400])
    ok, out = _git_check(integration_wt, "merge", "--no-edit", "--no-ff",
                         "FETCH_HEAD",
                         "-m", "merge: subtask from %s" %
                         Path(source_wt).name)
    if ok:
        return MergeOutcome(MergeOutcome.OK, out[:200])
    if "CONFLICT" in out or "conflict" in out.lower():
        # deterministically abort a conflicted merge state
        _git_check(integration_wt, "merge", "--abort")
        return MergeOutcome(MergeOutcome.CONFLICT, out[:400])
    return MergeOutcome(MergeOutcome.ERROR, out[:400])
