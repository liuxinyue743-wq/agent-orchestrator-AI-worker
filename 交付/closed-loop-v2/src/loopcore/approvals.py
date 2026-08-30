"""Bounded auto-approval: keep unattended workers from stalling on permission.

Ported from ao-supervision-sidecar closed_loop._maybe_auto_approve (the fix
that broke the "fully automatic" deadlock — real-run evidence: a worker
blocked on a pending Edit permission sat 30 minutes into a budget HUMAN).

Policy (must be user-approved per deployment):
  - file edits whose target resolves INSIDE the task's allowed_paths and NOT
    in forbidden_paths  -> resolve as allow_once;
  - command approvals only for the task's own gate commands, pytest
    invocations, and git bookkeeping inside the worker worktree;
  - everything else stays pending for the human (the human touchpoint).

The policy decision itself is a pure function (testable offline); the
AutoApprover applies it against a live AO client with idempotent dedup.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class ApprovalClient(Protocol):
    """The two AO calls the approver needs (AOClient satisfies this)."""

    def get_conversation(self, session_id: str, **kwargs: Any) -> dict: ...

    def resolve_approval(
        self, session_id: str, request_id: str, decision: str = "allow"
    ) -> bool: ...


class DedupStore(Protocol):
    def counter_get(self, key: str) -> int: ...

    def counter_set(self, key: str, value: int) -> None: ...


@dataclass(frozen=True)
class ApprovalDecision:
    request_id: str
    allow: bool
    reason: str


def path_matches(rel_path: str, patterns: list[str]) -> bool:
    """True when rel_path matches any glob or directory prefix pattern."""

    rel = rel_path.replace("\\", "/").lstrip("/")
    for pat in patterns or []:
        p = pat.replace("\\", "/").rstrip("/")
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p + "/*"):
            return True
    return False


def is_safe_command(cmd: str, gate_commands: list[str]) -> bool:
    """True when a command approval may be granted unattended.

    Allowed: the task's own gate commands (any -q/-v verbosity variant),
    pytest invocations, and read/add/commit git bookkeeping. Everything else
    (arbitrary shell, network, installs, deletion) stays pending.
    """

    cmd = " ".join((cmd or "").split())
    if not cmd:
        return False
    for gate in gate_commands or []:
        g = " ".join(str(gate).split())
        if not g:
            continue
        if (
            cmd == g
            or cmd.startswith(g + " ")
            or g.startswith(cmd + " ")
            or cmd.rstrip(" -v").rstrip(" -q") == g.rstrip(" -q")
        ):
            return True
    head = cmd.split(" ", 1)[0]
    if head in ("python", "python3", "py"):
        rest = cmd[len(head):].strip()
        if rest.startswith("-m pytest"):
            return True
    if head == "git":
        sub = cmd.split(" ")[1:2]
        if sub and sub[0] in (
            "add", "commit", "status", "diff", "log", "restore", "checkout",
        ):
            return True
    return False


def decide_approval(
    activity: dict[str, Any],
    *,
    allowed_paths: list[str],
    forbidden_paths: list[str],
    gate_commands: list[str],
    worktree_root: str = "",
) -> ApprovalDecision | None:
    """Pure policy for one pending approval activity.

    Returns None when the activity is not a pending approval request.
    """

    if (activity.get("activityKind") or activity.get("kind")) != "approval":
        return None
    if (activity.get("status") or "") != "pending":
        return None
    request_id = activity.get("providerItemId") or activity.get("id")
    if not request_id:
        return None

    detail = activity.get("detail") or {}
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            detail = {}
    inputs = detail.get("input") or {}
    file_path = inputs.get("file_path") or inputs.get("path") or ""

    if not file_path:
        command = str(inputs.get("command") or "")
        if command and is_safe_command(command, list(gate_commands)):
            return ApprovalDecision(request_id, True, "safe command")
        return ApprovalDecision(request_id, False, "arbitrary command -> human")

    rel = _rel_to_root(file_path, worktree_root)
    if path_matches(rel, list(forbidden_paths)):
        return ApprovalDecision(request_id, False, f"forbidden path: {rel}")
    if path_matches(rel, list(allowed_paths)):
        return ApprovalDecision(request_id, True, f"inside allowed_paths: {rel}")
    return ApprovalDecision(request_id, False, f"outside allowed_paths: {rel}")


def _rel_to_root(file_path: str, root: str) -> str:
    if root:
        try:
            return str(Path(file_path).resolve().relative_to(Path(root).resolve()))
        except (ValueError, OSError):
            pass
    return file_path


class AutoApprover:
    """Scan one worker's conversation and resolve policy-allowed approvals."""

    def __init__(
        self,
        client: ApprovalClient,
        store: DedupStore,
        *,
        task_id: str,
        worker_session_id: str,
        allowed_paths: list[str],
        forbidden_paths: list[str],
        gate_commands: list[str],
        worktree_root: str = "",
    ) -> None:
        self.client = client
        self.store = store
        self.task_id = task_id
        self.worker_session_id = worker_session_id
        self.allowed_paths = allowed_paths
        self.forbidden_paths = forbidden_paths
        self.gate_commands = gate_commands
        self.worktree_root = worktree_root

    def sweep(self) -> list[ApprovalDecision]:
        """One pass over pending approvals; returns decisions taken.

        Idempotent: each request id is deduped in the store; failures are
        recorded as -1 and never retried automatically (they stay pending
        for the human). Transport errors fail closed to an empty list.
        """

        try:
            conversation = self.client.get_conversation(self.worker_session_id)
        except Exception:
            return []
        acted: list[ApprovalDecision] = []
        for activity in conversation.get("activities") or []:
            decision = decide_approval(
                activity,
                allowed_paths=self.allowed_paths,
                forbidden_paths=self.forbidden_paths,
                gate_commands=self.gate_commands,
                worktree_root=self.worktree_root,
            )
            if decision is None:
                continue
            key = f"approved:{self.task_id}:{decision.request_id}"
            if self.store.counter_get(key) != 0:
                continue
            if decision.allow:
                ok = self.client.resolve_approval(
                    self.worker_session_id, decision.request_id, "allow"
                )
                self.store.counter_set(key, 1 if ok else -1)
                if ok:
                    acted.append(decision)
            else:
                # explicitly out of scope: record and leave pending for human
                self.store.counter_set(key, -1)
        return acted
