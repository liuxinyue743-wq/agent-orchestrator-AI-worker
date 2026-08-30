"""Loop Bus core: dedup inbox, budget control, and fail-closed routing.

Design baseline: ARCHITECTURE-v0.2.md sections 2.2-2.4.
The Bus is a deterministic program. It never calls an LLM; agent endpoints
are reached through registered transport handlers (AO chat sessions in
production, plain callables in tests).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .envelope import Envelope, MessageKind, Role

# Kinds that participate in dual-channel dedup (section 2.3): a Worker and the
# Supervisor station may both report the same underlying issue to the Planner.
_DEDUP_KINDS = frozenset(
    {
        MessageKind.BLOCKER_REPORT,
        MessageKind.CHECKER_REQUEST,
        MessageKind.ESCALATION,
        MessageKind.RISK_SIGNAL,
    }
)


class BusError(RuntimeError):
    """Deterministic Bus failure; callers must not guess a recovery."""


class RouteNotRegistered(BusError):
    """No handler registered for the envelope's receiver."""


class BudgetExceeded(BusError):
    """Thread exceeded hop / time budget; Bus must route to HUMAN instead."""


@dataclass(frozen=True)
class BusConfig:
    """Per-thread loop bounds. All values come from config/default.yaml and
    are applied exactly as the user sets them (only positivity is enforced)."""

    max_hops_per_thread: int = 24
    max_audits_per_thread: int = 3
    overall_timeout_seconds: float = 600.0

    def __post_init__(self) -> None:
        if self.max_hops_per_thread <= 0:
            raise ValueError("max_hops_per_thread must be positive")
        if self.max_audits_per_thread <= 0:
            raise ValueError("max_audits_per_thread must be positive")
        if self.overall_timeout_seconds <= 0:
            raise ValueError("overall_timeout_seconds must be positive")


@dataclass
class IssueRecord:
    """One deduplicated issue thread: every reporter and the final verdict."""

    fingerprint: str
    thread_id: str
    reporters: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    verdict: dict | None = None


@dataclass
class _ThreadState:
    first_seen_monotonic: float
    hops: int = 0
    audits: int = 0


Handler = Callable[[Envelope], None]


class LoopBus:
    """Deterministic message bus connecting all role endpoints."""

    def __init__(self, config: BusConfig | None = None) -> None:
        self.config = config or BusConfig()
        self._handlers: dict[str, Handler] = {}
        self._issues: dict[str, IssueRecord] = {}
        self._threads: dict[str, _ThreadState] = {}
        self._seen_idempotency_keys: set[str] = set()
        self._lock = threading.Lock()

    # ---------------------------------------------------------- endpoints
    def register(self, endpoint: str, handler: Handler) -> None:
        """Register a receiver endpoint: a role value or 'worker:<session>'."""

        if not endpoint or not endpoint.strip():
            raise ValueError("endpoint must be non-blank")
        with self._lock:
            if endpoint in self._handlers:
                raise BusError(f"endpoint already registered: {endpoint}")
            self._handlers[endpoint] = handler

    def has_endpoint(self, endpoint: str) -> bool:
        """Return True when an endpoint already has a registered handler."""

        with self._lock:
            return endpoint in self._handlers

    # ------------------------------------------------------------ intake
    def submit(self, envelope: Envelope) -> Envelope:
        """Accept one envelope, enforce budgets/dedup, and dispatch it.

        Returns the envelope actually delivered. Raises BudgetExceeded when the
        thread is out of bounds — the caller must then route a HUMAN envelope
        (the Bus emits one itself via the human handler if registered).
        """

        with self._lock:
            self._check_idempotent(envelope)
            self._check_budget(envelope)
            envelope = self._dedup(envelope)
            delivered = envelope.with_hop()
            state = self._threads[delivered.thread_id]
            state.hops += 1
            if delivered.kind is MessageKind.AUDIT_REQUEST:
                state.audits += 1
                if state.audits > self.config.max_audits_per_thread:
                    self._escalate_human(delivered, "max audits per thread exceeded")
                    raise BudgetExceeded("max audits per thread exceeded")
        handler = self._resolve_handler(delivered.receiver)
        handler(delivered)
        # Owner-ruled visibility: a USER_DIRECTIVE to any endpoint other than
        # the Planner is mirrored to the Planner as USER_DIRECTIVE_COPY, so
        # every user instruction is always visible to the Planner. Directives
        # addressed to the Planner stay private (no mirror, no copy).
        if (
            delivered.kind is MessageKind.USER_DIRECTIVE
            and delivered.receiver != Role.PLANNER.value
        ):
            planner_handler = self._handlers.get(Role.PLANNER.value)
            if planner_handler is not None:
                planner_handler(
                    Envelope(
                        sender=Role.USER.value,
                        receiver=Role.PLANNER.value,
                        kind=MessageKind.USER_DIRECTIVE_COPY,
                        thread_id=delivered.thread_id,
                        payload={
                            "originalReceiver": delivered.receiver,
                            "directive": delivered.payload.get("directive", ""),
                            "originalMsgId": delivered.msg_id,
                        },
                    )
                )
        return delivered

    # ------------------------------------------------------- dedup (2.3)
    def _dedup(self, envelope: Envelope) -> Envelope:
        if envelope.kind not in _DEDUP_KINDS:
            return envelope
        fingerprint = envelope.payload.get("issueFingerprint")
        if not fingerprint:
            raise BusError(
                f"{envelope.kind.value} requires payload.issueFingerprint"
            )
        record = self._issues.get(fingerprint)
        if record is None:
            self._issues[fingerprint] = IssueRecord(
                fingerprint=fingerprint,
                thread_id=envelope.thread_id,
                reporters=[envelope.sender],
                evidence=[str(envelope.payload.get("evidence", ""))],
            )
            return envelope
        # Same issue reported again (e.g. Worker AND Supervisor): merge into
        # the existing adjudication thread, do not trigger a second ruling.
        if envelope.sender not in record.reporters:
            record.reporters.append(envelope.sender)
        evidence = str(envelope.payload.get("evidence", ""))
        if evidence and evidence not in record.evidence:
            record.evidence.append(evidence)
        merged = Envelope(
            sender=envelope.sender,
            receiver=envelope.receiver,
            kind=envelope.kind,
            thread_id=record.thread_id,  # redirect to the existing thread
            payload={
                **envelope.payload,
                "mergedInto": record.thread_id,
                "reporters": list(record.reporters),
                "evidenceMerged": list(record.evidence),
            },
            msg_id=envelope.msg_id,
            idempotency_key=envelope.idempotency_key,
            hop=envelope.hop,
        )
        return merged

    def resolve_issue(self, fingerprint: str, verdict: dict) -> IssueRecord:
        """Record the Planner's verdict for one issue; reporters read it here."""

        with self._lock:
            record = self._issues.get(fingerprint)
            if record is None:
                raise BusError(f"unknown issue fingerprint: {fingerprint}")
            if record.verdict is not None:
                raise BusError(f"issue already adjudicated: {fingerprint}")
            record.verdict = verdict
            return record

    def issue(self, fingerprint: str) -> IssueRecord | None:
        with self._lock:
            return self._issues.get(fingerprint)

    # ------------------------------------------------------------ guards
    def _check_idempotent(self, envelope: Envelope) -> None:
        if envelope.idempotency_key in self._seen_idempotency_keys:
            raise BusError(
                f"duplicate idempotency key: {envelope.idempotency_key}"
            )
        self._seen_idempotency_keys.add(envelope.idempotency_key)

    def _check_budget(self, envelope: Envelope) -> None:
        now = time.monotonic()
        state = self._threads.get(envelope.thread_id)
        if state is None:
            state = _ThreadState(first_seen_monotonic=now)
            self._threads[envelope.thread_id] = state
        if envelope.hop >= self.config.max_hops_per_thread:
            self._escalate_human(envelope, "single message hop limit exceeded")
            raise BudgetExceeded("single message hop limit exceeded")
        if state.hops >= self.config.max_hops_per_thread:
            self._escalate_human(envelope, "max hops per thread exceeded")
            raise BudgetExceeded("max hops per thread exceeded")
        if now - state.first_seen_monotonic > self.config.overall_timeout_seconds:
            self._escalate_human(envelope, "overall thread timeout exceeded")
            raise BudgetExceeded("overall thread timeout exceeded")

    def _resolve_handler(self, receiver: str) -> Handler:
        handler = self._handlers.get(receiver)
        if handler is None:
            raise RouteNotRegistered(f"no handler for endpoint: {receiver}")
        return handler

    def _escalate_human(self, envelope: Envelope, reason: str) -> None:
        handler = self._handlers.get(Role.HUMAN.value)
        if handler is None:
            return
        handler(
            Envelope(
                sender=envelope.sender,
                receiver=Role.HUMAN.value,
                kind=MessageKind.HUMAN,
                thread_id=envelope.thread_id,
                payload={
                    "reason": reason,
                    "originalKind": envelope.kind.value,
                    "originalReceiver": envelope.receiver,
                },
            )
        )
