"""One-shot deterministic observation and trigger evaluation for AO Workers."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from numbers import Real

from .ao_client import AOClient


class ObserverError(RuntimeError):
    """AO returned state that cannot form a safe Worker observation."""


class ObserverTrigger(str, Enum):
    """Deterministic reasons for requesting a later semantic audit."""

    REPEATED_FAILURE = "REPEATED_FAILURE"
    MILESTONE = "MILESTONE"
    STALL = "STALL"


@dataclass(frozen=True, order=True)
class TurnObservation:
    """The stable identity and current state of one conversation turn."""

    id: str
    state: str


@dataclass(frozen=True, order=True)
class MessageObservation:
    """The progress-bearing fields of one conversation message."""

    id: str
    revision: int
    streaming: bool


@dataclass(frozen=True, order=True)
class ActivityObservation:
    """The progress-bearing fields of one conversation activity."""

    id: str
    revision: int
    activity_kind: str
    status: str


@dataclass(frozen=True, order=True)
class WorkspaceFileObservation:
    """The public path and state of one changed workspace file."""

    path: str
    status: str


@dataclass(frozen=True, order=True)
class FailureOccurrence:
    """One failure tied to a stable AO source identifier."""

    source_id: str
    fingerprint: str


@dataclass(frozen=True)
class Observation:
    """A read-only point-in-time view of one AO Chat-mode Worker."""

    worker_session_id: str
    project_id: str
    activity_state: str
    latest_sequence: int
    turns: tuple[TurnObservation, ...]
    messages: tuple[MessageObservation, ...]
    activities: tuple[ActivityObservation, ...]
    workspace_files: tuple[WorkspaceFileObservation, ...]
    commit_shas: tuple[str, ...]
    failures: tuple[FailureOccurrence, ...]

    @property
    def progress_signature(self) -> tuple[object, ...]:
        """Return state that represents effective progress, excluding sequence alone."""

        return (
            self.turns,
            self.messages,
            self.activities,
            self.workspace_files,
            self.commit_shas,
            self.failures,
        )


@dataclass(frozen=True)
class ObserverResult:
    """A deterministic trigger and its evidence, without an audit decision."""

    trigger: ObserverTrigger
    evidence: tuple[str, ...]


_TURN_STATES = frozenset(
    {"queued", "running", "completed", "recovered", "interrupted", "failed"}
)
_ACTIVITY_KINDS = frozenset(
    {
        "command",
        "file_change",
        "plan",
        "reasoning",
        "approval",
        "usage",
        "error",
        "system",
        "mcp_tool",
        "auto_review",
        "user_input",
    }
)
_ACTIVITY_STATUSES = frozenset(
    {
        "running",
        "completed",
        "recovered",
        "failed",
        "cancelled",
        "pending",
        "resolved",
    }
)
_MILESTONE_ACTIVITY_STATES = frozenset({"idle", "waiting_input"})


def normalize_failure_fingerprint(message: str) -> str:
    """Normalize one error with only case folding and whitespace collapsing."""

    if not isinstance(message, str):
        raise TypeError("message must be a string")
    return " ".join(message.casefold().strip().split())


def capture_observation(
    client: AOClient,
    worker_session_id: str,
) -> Observation:
    """Capture a Worker using only Session, Conversation, and workspace GETs."""

    if not isinstance(worker_session_id, str) or not worker_session_id:
        raise ValueError("worker_session_id must be a non-empty string")

    session = client.get_session(worker_session_id)
    project_id, activity_state = _validate_worker_session(
        session, worker_session_id
    )

    conversation = client.get_conversation(worker_session_id, limit=500)
    _require_matching_session_id(
        conversation, worker_session_id, "conversation snapshot"
    )
    latest_sequence = _require_non_negative_int(
        conversation.get("latestSequence"),
        "conversation latestSequence",
    )
    turns, turn_failures = _capture_turns(conversation.get("turns"))
    messages = _capture_messages(conversation.get("messages"))
    activities, activity_failures = _capture_activities(
        conversation.get("activities")
    )

    workspace = client.get_workspace_summary(worker_session_id)
    _require_matching_session_id(workspace, worker_session_id, "workspace summary")
    if workspace.get("truncated") is not False:
        raise ObserverError("workspace summary must be complete and not truncated")
    workspace_files = _capture_workspace_files(workspace.get("files"))
    commit_shas = _capture_commit_shas(workspace.get("commits"))

    return Observation(
        worker_session_id=worker_session_id,
        project_id=project_id,
        activity_state=activity_state,
        latest_sequence=latest_sequence,
        turns=turns,
        messages=messages,
        activities=activities,
        workspace_files=workspace_files,
        commit_shas=commit_shas,
        failures=tuple(sorted((*turn_failures, *activity_failures))),
    )


def evaluate_observation(
    previous: Observation,
    current: Observation,
    elapsed_seconds: float,
    *,
    repeated_failure_threshold: int = 2,
    stall_threshold_seconds: float = 300,
) -> ObserverResult | None:
    """Evaluate one observation pair with fixed trigger priority and no side effects."""

    _validate_evaluation_inputs(
        previous,
        current,
        elapsed_seconds,
        repeated_failure_threshold,
        stall_threshold_seconds,
    )

    repeated_failure = _evaluate_repeated_failure(
        previous, current, repeated_failure_threshold
    )
    if repeated_failure is not None:
        return repeated_failure

    previous_turn_states = {turn.id: turn.state for turn in previous.turns}
    completed_turn_ids = tuple(
        turn.id
        for turn in current.turns
        if turn.state == "completed"
        and previous_turn_states.get(turn.id) != "completed"
    )
    if (
        completed_turn_ids
        and current.activity_state in _MILESTONE_ACTIVITY_STATES
    ):
        return ObserverResult(
            trigger=ObserverTrigger.MILESTONE,
            evidence=(
                "new completed turn ids: " + ", ".join(completed_turn_ids),
                f"worker activity is {current.activity_state}",
            ),
        )

    if (
        previous.activity_state == "active"
        and current.activity_state == "active"
        and current.progress_signature == previous.progress_signature
        and elapsed_seconds >= stall_threshold_seconds
    ):
        return ObserverResult(
            trigger=ObserverTrigger.STALL,
            evidence=(
                "progress signature is unchanged",
                "progress remained unchanged for at least the stall threshold",
                f"stall threshold is {stall_threshold_seconds:g} seconds",
            ),
        )

    return None


def _validate_worker_session(
    session: object,
    worker_session_id: str,
) -> tuple[str, str]:
    if not isinstance(session, dict):
        raise ObserverError("session response must be an object")
    _require_matching_session_id(
        session,
        worker_session_id,
        "session response",
        id_key="id",
    )
    if session.get("kind") != "worker":
        raise ObserverError("target session must have kind 'worker'")
    if session.get("mode") != "chat":
        raise ObserverError("target Worker must use Chat mode")
    is_terminated = session.get("isTerminated")
    if not isinstance(is_terminated, bool):
        raise ObserverError("session isTerminated must be a boolean")
    if is_terminated or session.get("status") == "terminated":
        raise ObserverError("target Worker must not be terminated")
    project_id = _require_non_empty_string(
        session.get("projectId"), "session projectId"
    )
    activity = session.get("activity")
    if not isinstance(activity, dict):
        raise ObserverError("session activity must be an object")
    activity_state = _require_non_empty_string(
        activity.get("state"), "session activity state"
    )
    if activity_state in {"blocked", "exited"} or session.get("status") == "exited":
        raise ObserverError("target Worker is blocked or exited")
    return project_id, activity_state


def _capture_turns(
    value: object,
) -> tuple[tuple[TurnObservation, ...], tuple[FailureOccurrence, ...]]:
    items = _require_object_list(value, "conversation turns")
    turns: list[TurnObservation] = []
    failures: list[FailureOccurrence] = []
    seen_ids: set[str] = set()
    for item in items:
        turn_id = _require_non_empty_string(item.get("id"), "turn id")
        if turn_id in seen_ids:
            raise ObserverError(f"conversation contains duplicate turn id {turn_id!r}")
        seen_ids.add(turn_id)
        state = _require_non_empty_string(item.get("state"), "turn state")
        if state not in _TURN_STATES:
            raise ObserverError(f"turn {turn_id!r} has invalid state {state!r}")
        turns.append(TurnObservation(turn_id, state))

        error_message = item.get("errorMessage")
        if error_message is not None and not isinstance(error_message, str):
            raise ObserverError(f"turn {turn_id!r} errorMessage must be a string")
        if isinstance(error_message, str):
            fingerprint = normalize_failure_fingerprint(error_message)
            if fingerprint:
                failures.append(FailureOccurrence(f"turn:{turn_id}", fingerprint))

    return tuple(sorted(turns)), tuple(sorted(failures))


def _capture_messages(value: object) -> tuple[MessageObservation, ...]:
    items = _require_object_list(value, "conversation messages")
    messages: list[MessageObservation] = []
    seen_ids: set[str] = set()
    for item in items:
        message_id = _require_non_empty_string(item.get("id"), "message id")
        if message_id in seen_ids:
            raise ObserverError(
                f"conversation contains duplicate message id {message_id!r}"
            )
        seen_ids.add(message_id)
        revision = _require_non_negative_int(
            item.get("revision"), f"message {message_id!r} revision"
        )
        streaming = item.get("streaming")
        if not isinstance(streaming, bool):
            raise ObserverError(
                f"message {message_id!r} streaming must be a boolean"
            )
        messages.append(MessageObservation(message_id, revision, streaming))
    return tuple(sorted(messages))


def _capture_activities(
    value: object,
) -> tuple[tuple[ActivityObservation, ...], tuple[FailureOccurrence, ...]]:
    items = _require_object_list(value, "conversation activities")
    activities: list[ActivityObservation] = []
    failures: list[FailureOccurrence] = []
    seen_ids: set[str] = set()
    for item in items:
        activity_id = _require_non_empty_string(item.get("id"), "activity id")
        if activity_id in seen_ids:
            raise ObserverError(
                f"conversation contains duplicate activity id {activity_id!r}"
            )
        seen_ids.add(activity_id)
        revision = _require_non_negative_int(
            item.get("revision"), f"activity {activity_id!r} revision"
        )
        activity_kind = _require_non_empty_string(
            item.get("activityKind"), f"activity {activity_id!r} activityKind"
        )
        if activity_kind not in _ACTIVITY_KINDS:
            raise ObserverError(
                f"activity {activity_id!r} has invalid activityKind "
                f"{activity_kind!r}"
            )
        status = _require_non_empty_string(
            item.get("status"), f"activity {activity_id!r} status"
        )
        if status not in _ACTIVITY_STATUSES:
            raise ObserverError(
                f"activity {activity_id!r} has invalid status {status!r}"
            )
        summary = item.get("summary")
        if not isinstance(summary, str):
            raise ObserverError(
                f"activity {activity_id!r} summary must be a string"
            )
        detail = item.get("detail", {})
        if not isinstance(detail, dict):
            raise ObserverError(
                f"activity {activity_id!r} detail must be an object"
            )

        activities.append(
            ActivityObservation(activity_id, revision, activity_kind, status)
        )
        if activity_kind == "error" or status == "failed":
            failure_text = _first_non_empty_string(
                detail.get("output"),
                detail.get("error"),
                summary,
            )
            if failure_text is not None:
                failures.append(
                    FailureOccurrence(
                        f"activity:{activity_id}",
                        normalize_failure_fingerprint(failure_text),
                    )
                )

    return tuple(sorted(activities)), tuple(sorted(failures))


def _first_non_empty_string(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


def _capture_workspace_files(
    value: object,
) -> tuple[WorkspaceFileObservation, ...]:
    items = _require_object_list(value, "workspace files")
    files: list[WorkspaceFileObservation] = []
    seen_paths: set[str] = set()
    for item in items:
        path = _require_non_empty_string(item.get("path"), "workspace file path")
        if path in seen_paths:
            raise ObserverError(f"workspace contains duplicate file path {path!r}")
        seen_paths.add(path)
        status = _require_non_empty_string(
            item.get("status"), f"workspace file {path!r} status"
        )
        files.append(WorkspaceFileObservation(path, status))
    return tuple(sorted(files))


def _capture_commit_shas(value: object) -> tuple[str, ...]:
    items = _require_object_list(value, "workspace commits")
    shas: list[str] = []
    seen_shas: set[str] = set()
    for item in items:
        sha = _require_non_empty_string(item.get("sha"), "workspace commit sha")
        if sha in seen_shas:
            raise ObserverError(f"workspace contains duplicate commit sha {sha!r}")
        seen_shas.add(sha)
        shas.append(sha)
    return tuple(sorted(shas))


def _evaluate_repeated_failure(
    previous: Observation,
    current: Observation,
    threshold: int,
) -> ObserverResult | None:
    previous_by_source = {
        occurrence.source_id: occurrence.fingerprint
        for occurrence in previous.failures
    }
    current_by_source = {
        occurrence.source_id: occurrence.fingerprint
        for occurrence in current.failures
    }
    new_source_ids = set(current_by_source) - set(previous_by_source)
    if not new_source_ids:
        return None

    combined_by_source = dict(previous_by_source)
    combined_by_source.update(current_by_source)
    counts = Counter(combined_by_source.values())
    candidates = sorted(
        fingerprint
        for fingerprint, count in counts.items()
        if count >= threshold
        and any(
            current_by_source[source_id] == fingerprint
            for source_id in new_source_ids
        )
    )
    if not candidates:
        return None

    fingerprint = candidates[0]
    matching_new_sources = sorted(
        source_id
        for source_id in new_source_ids
        if current_by_source[source_id] == fingerprint
    )
    return ObserverResult(
        trigger=ObserverTrigger.REPEATED_FAILURE,
        evidence=(
            f"failure fingerprint: {fingerprint}",
            f"occurrence count: {counts[fingerprint]}",
            "new source ids: " + ", ".join(matching_new_sources),
        ),
    )


def _validate_evaluation_inputs(
    previous: Observation,
    current: Observation,
    elapsed_seconds: float,
    repeated_failure_threshold: int,
    stall_threshold_seconds: float,
) -> None:
    if not isinstance(previous, Observation) or not isinstance(current, Observation):
        raise TypeError("previous and current must be Observation instances")
    if previous.worker_session_id != current.worker_session_id:
        raise ValueError("observations must belong to the same Worker session")
    if previous.project_id != current.project_id:
        raise ValueError("observations must belong to the same project")
    if (
        not isinstance(elapsed_seconds, Real)
        or isinstance(elapsed_seconds, bool)
        or elapsed_seconds < 0
    ):
        raise ValueError("elapsed_seconds must be a non-negative number")
    if (
        not isinstance(repeated_failure_threshold, int)
        or isinstance(repeated_failure_threshold, bool)
        or repeated_failure_threshold < 1
    ):
        raise ValueError("repeated_failure_threshold must be an integer >= 1")
    if (
        not isinstance(stall_threshold_seconds, Real)
        or isinstance(stall_threshold_seconds, bool)
        or stall_threshold_seconds < 0
    ):
        raise ValueError("stall_threshold_seconds must be a non-negative number")


def _require_matching_session_id(
    payload: object,
    expected: str,
    description: str,
    *,
    id_key: str = "sessionId",
) -> None:
    if not isinstance(payload, dict):
        raise ObserverError(f"{description} must be an object")
    actual = payload.get(id_key)
    if actual != expected:
        raise ObserverError(f"{description} session id does not match the target")


def _require_object_list(
    value: object,
    description: str,
) -> list[dict[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ObserverError(f"{description} must be a list of objects")
    return value


def _require_non_empty_string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise ObserverError(f"{description} must be a non-empty string")
    return value


def _require_non_negative_int(value: object, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ObserverError(f"{description} must be a non-negative integer")
    return value
