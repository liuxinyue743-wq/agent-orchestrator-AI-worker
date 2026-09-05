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
import os
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


def _git(worktree: str, *args: str, timeout: int = 30) -> Optional[str]:
    """Run a read-only git command; None on ANY failure (fail-closed).

    Previously every error (missing repo, timeout, non-zero exit) was
    swallowed into "" which downstream read as "no changes" — the path gate
    and the Verifier then waved through unaudited work (review 簇四).
    """
    try:
        proc = subprocess.run(["git", "-C", worktree, *args],
                              capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            return None
        return proc.stdout or ""
    except Exception:
        return None


def _current_head(worktree: str) -> Optional[str]:
    out = _git(worktree, "rev-parse", "HEAD")
    if not out:
        return None
    return out.strip() or None


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
        # Counter says frozen: the sidecar MUST hold the SHA. A crash between
        # the two writes used to leave base "" (fail-open); write order is
        # now sidecar-first, and an unreadable sidecar returns "" so callers
        # escalate instead of diffing against nothing.
        return _read_base_sidecar(worktree, tag)
    head = _current_head(worktree)
    if not head:
        return ""
    _ensure_info_exclude(worktree)
    _write_base_sidecar(worktree, tag, head)
    store.counter_set(key, 1)
    return head


def _ensure_info_exclude(worktree: str) -> None:
    """Best-effort: keep Python build artifacts out of WORKER commits by
    listing them in .git/info/exclude. Unlike .gitignore this file is
    per-worktree and untracked, so it never pollutes the worker's diff —
    and `git add -A` by the worker then never sweeps __pycache__/*.pyc into
    history (the merge-time binary-conflict source; review 簇七)."""
    try:
        git_dir = _git(worktree, "rev-parse", "--git-dir")
        if not git_dir:
            return
        gd = Path(worktree) / git_dir.strip()
        if not gd.is_absolute():
            gd = (Path(worktree) / git_dir.strip()).resolve()
        info = gd / "info"
        info.mkdir(parents=True, exist_ok=True)
        excl = info / "exclude"
        patterns = ["__pycache__/", "*.pyc", "*.pyo", ".pytest_cache/",
                    ".mypy_cache/", ".ruff_cache/"]
        existing = excl.read_text(encoding="utf-8", errors="replace") \
            if excl.exists() else ""
        add = [p for p in patterns if p not in existing]
        if add:
            with open(excl, "a", encoding="utf-8") as f:
                f.write("\n# loopcore: keep build artifacts out of commits\n")
                f.write("\n".join(add) + "\n")
    except Exception:
        pass  # best-effort; the diff-level excludes remain as backstop


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


def changed_paths(worktree: str, base_commit: str) -> Optional[List[str]]:
    """Full changed-path set relative to base_commit; None on git failure
    (fail-closed: callers must treat None as 'unknown', never as 'clean').

    Covers: modified/added/deleted/renamed (tracked) + untracked new files.
    """
    paths: set = set()
    # committed changes vs base
    if base_commit:
        out = _git(worktree, "diff", "--name-status", base_commit + "...HEAD")
        if out is None:
            return None
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
        if out is None:
            return None
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                paths.add(parts[-1].strip())
                if parts[0].startswith("R") and len(parts) >= 3:
                    paths.add(parts[-1].strip())
    # untracked files (worker created new files)
    out = _git(worktree, "ls-files", "--others", "--exclude-standard")
    if out is None:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line:
            paths.add(line)
    return sorted(p for p in paths if p and not _is_artifact(p))


_ARTIFACT_MARKERS = (
    "__pycache__", ".pyc", ".pyo", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".coverage", ".tox", ".hypothesis", ".eggs",
)

# git pathspec excludes mirroring _ARTIFACT_MARKERS, for diff commands.
_ARTIFACT_EXCLUDES = (
    ":(exclude)**/__pycache__/**",
    ":(exclude)__pycache__/**",
    ":(exclude)**/*.pyc",
    ":(exclude)**/*.pyo",
    ":(exclude)**/.pytest_cache/**",
    ":(exclude).pytest_cache/**",
    ":(exclude)**/.mypy_cache/**",
    ":(exclude)**/.ruff_cache/**",
    ":(exclude)**/.hypothesis/**",
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


@dataclass(frozen=True)
class GitStateSnapshot:
    """Content-sensitive, read-only state of a Git worktree.

    ``digest`` distinguishes HEAD, the index, tracked working-tree content,
    and non-artifact untracked path/content.  It intentionally omits the
    underlying diffs so Gate evidence stays bounded.
    """

    head: str
    digest: str
    clean: bool


class GitStateSnapshotError(RuntimeError):
    """A required Git probe, parse, or untracked-file read failed."""


def _snapshot_git(worktree: str, *args: str, timeout: int = 30) -> bytes:
    """Run one required read-only Git probe and return its exact bytes."""
    try:
        proc = subprocess.run(
            ["git", "-C", worktree, *args],
            capture_output=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except Exception as exc:
        raise GitStateSnapshotError(
            "git %s could not run: %s" %
            (" ".join(args[:3]), str(exc)[:500])) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"").decode(
            "utf-8", errors="replace").strip()
        raise GitStateSnapshotError(
            "git %s exited with code %d%s" %
            (" ".join(args[:3]), proc.returncode,
             (": " + detail[:500]) if detail else ""))
    return proc.stdout or b""


def _nul_paths(raw: bytes, probe: str) -> List[str]:
    if raw and not raw.endswith(b"\0"):
        raise GitStateSnapshotError("%s returned malformed path data" % probe)
    try:
        return [os.fsdecode(item) for item in raw.split(b"\0") if item]
    except Exception as exc:
        raise GitStateSnapshotError(
            "%s path decoding failed: %s" % (probe, str(exc)[:500])) from exc


def _non_artifact_diff(worktree: str, *, cached: bool) -> tuple[bytes, bytes]:
    """Return (path-list bytes, full diff bytes) for one Git state layer."""
    layer = "cached" if cached else "unstaged"
    prefix = ["diff"] + (["--cached"] if cached else [])
    raw_paths = _snapshot_git(
        worktree, *prefix, "--name-only", "-z", "--no-renames",
        "--no-ext-diff", "--no-textconv", "--")
    paths = sorted(set(
        path for path in _nul_paths(raw_paths, "git %s path probe" % layer)
        if not _is_artifact(path)))
    framed_paths = b"\0".join(os.fsencode(path) for path in paths)
    if not paths:
        return framed_paths, b""
    pathspecs = [":(literal)%s" % path for path in paths]
    diff = _snapshot_git(
        worktree, *prefix, "--no-renames", "--no-ext-diff",
        "--no-textconv", "--binary", "--full-index", "--", *pathspecs)
    return framed_paths, diff


def _hash_frame(hasher, label: bytes, payload: bytes) -> None:
    hasher.update(label)
    hasher.update(len(payload).to_bytes(8, byteorder="big"))
    hasher.update(payload)


def git_state_snapshot(worktree: str) -> GitStateSnapshot:
    """Capture Gate-relevant repository state without changing it.

    Required probes fail explicitly.  Tracked artifact paths and untracked
    test/build artifacts use the same ``_is_artifact`` policy as the existing
    path gate.  External diff drivers and textconv are disabled.
    """
    root = Path(worktree)
    raw_head = _snapshot_git(worktree, "rev-parse", "HEAD")
    try:
        head = raw_head.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise GitStateSnapshotError(
            "git rev-parse HEAD returned non-ASCII data") from exc
    if (len(head) not in (40, 64) or
            any(ch not in "0123456789abcdefABCDEF" for ch in head)):
        raise GitStateSnapshotError(
            "git rev-parse HEAD did not return one commit SHA")

    cached_paths, cached_diff = _non_artifact_diff(worktree, cached=True)
    unstaged_paths, unstaged_diff = _non_artifact_diff(
        worktree, cached=False)
    raw_untracked = _snapshot_git(
        worktree, "ls-files", "--others", "--exclude-standard", "-z", "--")
    untracked_paths = sorted(set(
        path for path in _nul_paths(raw_untracked, "git untracked path probe")
        if not _is_artifact(path)))

    untracked = hashlib.sha256()
    for path in untracked_paths:
        normalized = path.replace("\\", "/")
        if (normalized.startswith("/") or
                any(part == ".." for part in normalized.split("/"))):
            raise GitStateSnapshotError(
                "unsafe untracked path returned by Git: %s" % path[:300])
        candidate = root / Path(path)
        try:
            mode = candidate.lstat().st_mode
            if stat.S_ISLNK(mode):
                kind = b"symlink"
                content = os.fsencode(os.readlink(candidate))
            elif stat.S_ISREG(mode):
                kind = b"file"
                content = candidate.read_bytes()
            else:
                raise GitStateSnapshotError(
                    "unsupported untracked path type: %s" % path[:300])
        except GitStateSnapshotError:
            raise
        except Exception as exc:
            raise GitStateSnapshotError(
                "unable to read untracked path %s: %s" %
                (path[:300], str(exc)[:500])) from exc
        _hash_frame(untracked, b"path", os.fsencode(normalized))
        _hash_frame(untracked, b"kind", kind)
        _hash_frame(untracked, b"content-sha256",
                    hashlib.sha256(content).digest())

    state = hashlib.sha256()
    _hash_frame(state, b"head", head.encode("ascii"))
    _hash_frame(state, b"cached-paths", cached_paths)
    _hash_frame(state, b"cached-diff-sha256",
                hashlib.sha256(cached_diff).digest())
    _hash_frame(state, b"unstaged-paths", unstaged_paths)
    _hash_frame(state, b"unstaged-diff-sha256",
                hashlib.sha256(unstaged_diff).digest())
    _hash_frame(state, b"untracked-sha256", untracked.digest())
    clean = not (cached_paths or unstaged_paths or untracked_paths)
    return GitStateSnapshot(head=head, digest=state.hexdigest(), clean=clean)


def diff_fingerprint(worktree: str, base_commit: str) -> str:
    """Stable fingerprint of the current change set + HEAD.

    Changes when the worker edits, commits, or reverts any file; stable on
    repeated reads of an unchanged tree. Used to detect thrash (same diff
    reappearing / being undone).
    """
    paths = changed_paths(worktree, base_commit)
    head = _current_head(worktree) or ""
    if paths is None:
        # git failed: a constant error marker keeps the fingerprint stable
        # across reads (no false thrash) but distinct from any real change.
        raw = "GIT-ERROR\n" + head
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()
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
    if changed is None:
        # fail-closed: an unauditable tree must never read as "clean"
        return ["<git-error: changed paths unavailable>"], []
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


def _head_tail(text: str, limit: int) -> str:
    """Truncate keeping BOTH ends: the head carries the diff headers/stat,
    the tail carries the final hunks — plain [:limit] slicing used to drop
    the verdict-critical tail of a large diff entirely (review: verifier
    evidence)."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.65)
    tail = limit - head - 64
    return (text[:head]
            + "\n[loopcore] ... %d chars elided ...\n"
              % (len(text) - head - tail)
            + text[-tail:])


def git_diff_text(worktree: str, base_commit: str, limit: int = 12000) -> str:
    """Diff text for the Auditor/Verifier, relative to base_commit.

    Must cover EVERYTHING the worker changed since the base:
      - the committed range base..HEAD (workers often `git commit` mid-task,
        which a plain working-tree diff would hide), and
      - untracked NEW files (git diff does not show them at all) — staged
        into the index just for the diff, then unstaged.
    """
    if not base_commit:
        out = _git(worktree, "diff")
        return ("[loopcore] git diff unavailable (error)\n"
                if out is None else _head_tail(out, limit))
    # Committed-range diffs must exclude build artifacts too: workers commit
    # with `git add -A`, which sweeps __pycache__/*.pyc into their commits,
    # and the Verifier then reads "binary file changed" hunks as out-of-scope
    # edits (real-run evidence: MISSION-QUICK-012 S1 verifier FAILed a fully
    # correct delivery three times over committed .pyc noise).
    scope = ["--", "."] + list(_ARTIFACT_EXCLUDES)
    committed = _git(worktree, "diff", base_commit + "...HEAD", *scope)
    working = _git(worktree, "diff", base_commit, *scope)
    if committed is None or working is None:
        # fail-closed: the Auditor/Verifier must SEE that evidence is missing
        return "[loopcore] git diff unavailable (git error)\n"
    out = committed + "\n" + working
    untracked_raw = _git(worktree, "ls-files", "--others",
                         "--exclude-standard")
    if untracked_raw is None:
        return "[loopcore] git diff unavailable (git error)\n"
    untracked = [p for p in untracked_raw.splitlines()
                 if p and not _is_artifact(p)]
    if untracked:
        _git(worktree, "add", "-N", "--", *untracked)
        try:
            staged = _git(worktree, "diff", "--", *untracked)
            out += "\n" + (staged or "")
        finally:
            # Always undo the intent-to-add entries; if this is skipped (e.g.
            # diff raised) the phantom entries linger in the index and pollute
            # later changed_paths / Verifier diffs.
            _git(worktree, "reset", "-q", "--", *untracked)
    return _head_tail(out, limit)


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


def commit_all(worktree: str, message: str) -> str:
    """Sidecar-side commit of everything in the worker's worktree.

    Called ONLY by trusted controller code (never the worker) so the merge
    pipeline has a clean commit to fetch. A clean worktree returns its current
    HEAD. Materialization gets one short retry for transient local Git
    failures; persistent failures raise with bounded, stage-specific Git
    output so the controller can preserve the real evidence.
    """
    head = _current_head(worktree)
    if not head:
        raise RuntimeError("git inspection failed: unable to read current HEAD")

    # changed_paths already filters artifacts and honors Git's ignore policy
    # for untracked files. Stage only those exact materializable paths: adding
    # "." plus explicit artifact exclusions makes Git reject an ignored
    # __pycache__ directory on real AO worktrees (MISSION-PANEL-20260901-200228).
    changed = changed_paths(worktree, head)
    if changed is None:
        raise RuntimeError(
            "git inspection failed: unable to inspect Worker changes")
    if not changed:
        return head
    pathspecs = [":(literal)%s" % path for path in changed]

    last_stage = "git commit"
    last_detail = ""
    for attempt in range(2):
        ok, detail = _git_check(worktree, "add", "-A", "--", *pathspecs)
        if not ok:
            last_stage, last_detail = "git add", detail
        else:
            ok, detail = _git_check(worktree, "commit", "-q", "-m",
                                    message)
            if ok:
                committed_head = _current_head(worktree)
                if not committed_head:
                    raise RuntimeError(
                        "git commit succeeded but reading HEAD failed")
                return committed_head
            last_stage, last_detail = "git commit", detail

        if attempt == 0:
            time.sleep(0.5)

    detail = (last_detail or "<no git output>").strip()
    raise RuntimeError("%s failed: %s" %
                       (last_stage, _head_tail(detail, 1000)))


def _main_head(repo_path: str) -> Optional[str]:
    """HEAD of the MAIN worktree of the repo containing `repo_path`.

    `git worktree list --porcelain` always lists the main worktree first.
    """
    out = _git(repo_path, "worktree", "list", "--porcelain")
    if not out:
        return None
    for line in out.splitlines():
        if line.startswith("HEAD "):
            return line.split(None, 1)[1].strip() or None
    return None


def add_integration_worktree(repo_path: str, branch: str,
                             target_path: str) -> Optional[str]:
    """Create (or reuse) an integration worktree for a mission.

    The new branch starts at the MAIN worktree's HEAD — never at a worker
    worktree's HEAD. Branching from the first-finished worker used to bake
    that subtask's whole delivery into the mission "base", so the final
    mission diff showed only the LAST merged subtask (root cause of
    MISSION-QUICK-010's phantom 'square missing' verdict).
    """
    Path(target_path).mkdir(parents=True, exist_ok=True)
    start = _main_head(repo_path)
    args = ["worktree", "add", "--checkout", "-B", branch, target_path]
    if start:
        args.append(start)
    ok, out = _git_check(repo_path, *args)
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
