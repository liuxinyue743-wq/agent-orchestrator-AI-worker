"""Loop Bus message envelope: the single wire format for all inter-role channels.

Design baseline: ../../ARCHITECTURE-v0.2.md sections 2.1-2.4.
Transport idempotency (clientMessageId, duplicate recovery) lives in
ao_client.py; this module defines the typed payload carried over it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Role(str, Enum):
    """Every endpoint on the Loop Bus. Workers carry their session id."""

    PLANNER = "planner"
    AUDITOR = "auditor"
    VERIFIER = "verifier"
    OBSERVER = "observer"  # deterministic program, not an agent
    GATE = "gate"  # deterministic program, not an agent
    HUMAN = "human"
    USER = "user"

    @staticmethod
    def worker(session_id: str) -> str:
        if not session_id or not session_id.strip():
            raise ValueError("worker session_id must be non-blank")
        return f"worker:{session_id}"

    @staticmethod
    def parse(value: str) -> "Role | str":
        """Return a Role for fixed roles, or the raw 'worker:<id>' string."""
        try:
            return Role(value)
        except ValueError:
            if value.startswith("worker:") and len(value) > len("worker:"):
                return value
            raise ValueError(f"unknown role endpoint: {value!r}")


class MessageKind(str, Enum):
    """Channel types. Direction matrix: ARCHITECTURE-v0.2.md section 2.2."""

    TASK_DISPATCH = "TASK_DISPATCH"  # Planner -> Worker
    LOCAL_FIX = "LOCAL_FIX"  # Planner/Auditor -> Worker (L0)
    REPLAN_DISPATCH = "REPLAN_DISPATCH"  # Planner -> Worker
    AUDIT_REQUEST = "AUDIT_REQUEST"  # Planner -> Auditor
    PV_TASK = "PV_TASK"  # Planner -> Verifier
    WATCH_DIRECTIVE = "WATCH_DIRECTIVE"  # Planner -> Observer
    GATE_RUN = "GATE_RUN"  # Planner -> Gate
    BLOCKER_REPORT = "BLOCKER_REPORT"  # Worker -> Planner
    CHECKER_REQUEST = "CHECKER_REQUEST"  # Worker -> Planner
    STATUS_CLAIM = "STATUS_CLAIM"  # Worker -> Auditor
    VERIFY_REQUEST = "VERIFY_REQUEST"  # Worker -> Verifier
    STATUS_NOTE = "STATUS_NOTE"  # Worker -> Observer
    AUDIT_REPORT = "AUDIT_REPORT"  # Auditor -> Planner
    ESCALATION = "ESCALATION"  # Auditor -> Planner
    AUDIT_QUERY = "AUDIT_QUERY"  # Auditor -> Worker
    AUDIT_VERIFY_REQUEST = "AUDIT_VERIFY_REQUEST"  # Auditor -> Verifier (one-way)
    FOCUS_WATCH = "FOCUS_WATCH"  # Auditor -> Observer
    PV_RESULT = "PV_RESULT"  # Verifier -> Planner
    VERDICT = "VERDICT"  # Verifier -> Planner
    FIX_REQUEST = "FIX_REQUEST"  # Verifier -> Worker
    RISK_SIGNAL = "RISK_SIGNAL"  # Observer -> Planner
    TRIGGER = "TRIGGER"  # Observer -> Auditor
    TRIGGER_VERIFY = "TRIGGER_VERIFY"  # Observer -> Verifier (one-way)
    STALL_NOTICE = "STALL_NOTICE"  # Observer -> Worker (reminder, not a command)
    GATE_EVIDENCE = "GATE_EVIDENCE"  # Gate -> Planner
    HUMAN = "HUMAN"  # any -> Human
    MEMORY_UPDATE = "MEMORY_UPDATE"  # Planner -> Bus (memory.md/project.md)
    FINAL_REPORT = "FINAL_REPORT"  # Planner -> user (via Bus)
    USER_DIRECTIVE = "USER_DIRECTIVE"  # user -> any agent
    USER_DIRECTIVE_COPY = "USER_DIRECTIVE_COPY"  # auto-mirror -> Planner


# Pairwise direction matrix ruled by the project owner (2026-08-30):
# Auditor->Verifier and Observer->Verifier are ONE-WAY; every other pair of
# roles is bidirectional. Programs (observer/gate) only emit signals and only
# receive directives. Worker endpoints match by the 'worker:' prefix.
_ALLOWED_ROUTES: frozenset[tuple[str, str, str]] = frozenset(
    {
        # Planner <-> Worker (bidirectional)
        ("planner", "TASK_DISPATCH", "worker:"),
        ("planner", "LOCAL_FIX", "worker:"),
        ("planner", "REPLAN_DISPATCH", "worker:"),
        ("worker:", "BLOCKER_REPORT", "planner"),
        ("worker:", "CHECKER_REQUEST", "planner"),
        # Planner <-> Auditor (bidirectional)
        ("planner", "AUDIT_REQUEST", "auditor"),
        ("auditor", "AUDIT_REPORT", "planner"),
        ("auditor", "ESCALATION", "planner"),
        # Planner <-> Verifier (bidirectional)
        ("planner", "PV_TASK", "verifier"),
        ("verifier", "PV_RESULT", "planner"),
        ("verifier", "VERDICT", "planner"),
        # Planner <-> Observer (bidirectional)
        ("planner", "WATCH_DIRECTIVE", "observer"),
        ("observer", "RISK_SIGNAL", "planner"),
        # Planner <-> Gate (bidirectional)
        ("planner", "GATE_RUN", "gate"),
        ("gate", "GATE_EVIDENCE", "planner"),
        # Worker <-> Auditor (bidirectional)
        ("worker:", "STATUS_CLAIM", "auditor"),
        ("auditor", "LOCAL_FIX", "worker:"),
        ("auditor", "AUDIT_QUERY", "worker:"),
        # Worker <-> Verifier (bidirectional)
        ("worker:", "VERIFY_REQUEST", "verifier"),
        ("verifier", "FIX_REQUEST", "worker:"),
        # Worker <-> Observer (bidirectional)
        ("worker:", "STATUS_NOTE", "observer"),
        ("observer", "STALL_NOTICE", "worker:"),
        # Auditor <-> Observer (bidirectional)
        ("auditor", "FOCUS_WATCH", "observer"),
        ("observer", "TRIGGER", "auditor"),
        # Auditor -> Verifier (ONE-WAY: results flow back only via Planner)
        ("auditor", "AUDIT_VERIFY_REQUEST", "verifier"),
        # Observer -> Verifier (ONE-WAY: no reply path)
        ("observer", "TRIGGER_VERIFY", "verifier"),
        # Planner -> Bus internals
        ("planner", "MEMORY_UPDATE", "bus"),
        ("planner", "FINAL_REPORT", "user"),
        # User direct channels (owner-ruled 2026-08-30): the user may address
        # ANY agent. Directives to the Planner stay private; directives to any
        # other endpoint are auto-mirrored to the Planner (see LoopBus.submit).
        ("user", "USER_DIRECTIVE", "planner"),
        ("user", "USER_DIRECTIVE", "auditor"),
        ("user", "USER_DIRECTIVE", "verifier"),
        ("user", "USER_DIRECTIVE", "observer"),
        ("user", "USER_DIRECTIVE", "gate"),
        ("user", "USER_DIRECTIVE", "worker:"),
        ("user", "USER_DIRECTIVE_COPY", "planner"),
    }
)


class Decision(str, Enum):
    """Auditor/Planner verdicts (CL-AO compatible)."""

    PASS = "PASS"
    LOCAL_FIX = "LOCAL_FIX"
    REPLAN = "REPLAN"
    HUMAN = "HUMAN"
    ESCALATE = "ESCALATE"


def issue_fingerprint(task_id: str, error_text: str, source: str) -> str:
    """Stable dedup key for dual-channel submissions (section 2.3).

    Normalization mirrors CL-AO: case-fold + whitespace collapse; the source
    namespace ('turn:' / 'activity:' / 'worker:' / 'observer:') stays distinct.
    """

    if not task_id.strip() or not source.strip():
        raise ValueError("task_id and source must be non-blank")
    normalized = " ".join(error_text.casefold().strip().split())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{source}:{task_id}:{digest}"


@dataclass(frozen=True)
class Envelope:
    """One typed message on the Loop Bus."""

    sender: str
    receiver: str
    kind: MessageKind
    thread_id: str
    payload: dict[str, Any]
    msg_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    idempotency_key: str = ""
    hop: int = 0

    def __post_init__(self) -> None:
        Role.parse(self.sender)
        if self.receiver not in ("bus", "user"):
            Role.parse(self.receiver)
        if not isinstance(self.kind, MessageKind):
            raise ValueError("kind must be a MessageKind")
        if not self.thread_id.strip():
            raise ValueError("thread_id must be non-blank")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be a dict")
        if self.hop < 0:
            raise ValueError("hop must be >= 0")
        if not self.idempotency_key:
            object.__setattr__(
                self,
                "idempotency_key",
                _full_idempotency_key(
                    {
                        "threadId": self.thread_id,
                        "from": self.sender,
                        "kind": self.kind.value,
                        "payload": self.payload,
                    }
                ),
            )
        self._check_route()

    def _check_route(self) -> None:
        sender_role = self.sender if not self.sender.startswith("worker:") else "worker:"
        receiver_role = (
            self.receiver if not self.receiver.startswith("worker:") else "worker:"
        )
        probe = (sender_role, self.kind.value, receiver_role)
        # HUMAN from anyone is always allowed.
        if self.kind is MessageKind.HUMAN and self.receiver == Role.HUMAN.value:
            return
        if probe not in _ALLOWED_ROUTES:
            raise ValueError(
                f"route not allowed: {sender_role} -[{self.kind.value}]-> {receiver_role}"
            )

    def with_hop(self) -> "Envelope":
        """Return a copy with hop+1 (Bus increments on each forward)."""

        return Envelope(
            sender=self.sender,
            receiver=self.receiver,
            kind=self.kind,
            thread_id=self.thread_id,
            payload=self.payload,
            msg_id=self.msg_id,
            idempotency_key=self.idempotency_key,
            hop=self.hop + 1,
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "msgId": self.msg_id,
                "threadId": self.thread_id,
                "from": self.sender,
                "to": self.receiver,
                "kind": self.kind.value,
                "payload": self.payload,
                "idempotencyKey": self.idempotency_key,
                "hop": self.hop,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, document: str) -> "Envelope":
        payload = json.loads(document)
        if not isinstance(payload, dict):
            raise ValueError("Envelope JSON must be an object")
        return cls(
            sender=payload["from"],
            receiver=payload["to"],
            kind=MessageKind(payload["kind"]),
            thread_id=payload["threadId"],
            payload=payload.get("payload", {}),
            msg_id=payload.get("msgId") or str(uuid.uuid4()),
            idempotency_key=payload.get("idempotencyKey", ""),
            hop=int(payload.get("hop", 0)),
        )


def _default_idempotency_key(thread_id: str, sender: str, kind: MessageKind) -> str:
    """Legacy key head kept for API compatibility; see _full_key below."""

    raw = f"{thread_id}|{sender}|{kind.value}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _full_idempotency_key(env_fields: dict[str, Any]) -> str:
    """Deterministic key: identical thread+sender+kind+payload is a duplicate
    (CL-AO strict-equal-body discipline); a changed payload is a new message."""

    canonical = json.dumps(
        env_fields, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
