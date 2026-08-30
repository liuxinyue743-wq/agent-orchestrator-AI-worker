from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest

from closed_loop_agent_orchestrator.ao_client import AOClient
from closed_loop_agent_orchestrator.observer import (
    ActivityObservation,
    FailureOccurrence,
    MessageObservation,
    Observation,
    ObserverError,
    ObserverTrigger,
    TurnObservation,
    WorkspaceFileObservation,
    capture_observation,
    evaluate_observation,
    normalize_failure_fingerprint,
)


WORKER_ID = "worker-1"
PROJECT_ID = "project-1"


def write_runfile(path: Path) -> Path:
    path.write_text(json.dumps({"pid": 123, "port": 4567}), encoding="utf-8")
    return path


def session(*, activity: str = "active") -> dict[str, Any]:
    return {
        "id": WORKER_ID,
        "projectId": PROJECT_ID,
        "kind": "worker",
        "mode": "chat",
        "isTerminated": False,
        "activity": {"state": activity},
        "status": activity,
    }


def conversation() -> dict[str, Any]:
    return {
        "sessionId": WORKER_ID,
        "latestSequence": 9,
        "oldestSequence": 1,
        "hasMoreBefore": False,
        "turns": [
            {"id": "turn-1", "state": "completed"},
            {
                "id": "turn-2",
                "state": "failed",
                "errorMessage": "  PROVIDER\n  Failed  ",
            },
        ],
        "messages": [
            {
                "id": "message-1",
                "revision": 2,
                "streaming": False,
            }
        ],
        "activities": [
            {
                "kind": "activity",
                "id": "activity-1",
                "revision": 3,
                "activityKind": "command",
                "status": "completed",
                "summary": "pytest passed",
                "detail": {"output": "1 passed"},
            }
        ],
    }


def workspace() -> dict[str, Any]:
    return {
        "sessionId": WORKER_ID,
        "files": [{"path": "src/example.py", "status": "modified"}],
        "commits": [{"sha": "abc123"}],
        "truncated": False,
    }


def observation(
    *,
    activity: str = "active",
    turns: tuple[TurnObservation, ...] = (TurnObservation("turn-1", "running"),),
    messages: tuple[MessageObservation, ...] = (
        MessageObservation("message-1", 1, True),
    ),
    activities: tuple[ActivityObservation, ...] = (
        ActivityObservation("activity-1", 1, "command", "running"),
    ),
    files: tuple[WorkspaceFileObservation, ...] = (),
    commits: tuple[str, ...] = (),
    failures: tuple[FailureOccurrence, ...] = (),
    latest_sequence: int = 1,
) -> Observation:
    return Observation(
        worker_session_id=WORKER_ID,
        project_id=PROJECT_ID,
        activity_state=activity,
        latest_sequence=latest_sequence,
        turns=turns,
        messages=messages,
        activities=activities,
        workspace_files=files,
        commit_shas=commits,
        failures=failures,
    )


def activity_item(
    activity_id: str,
    *,
    revision: object = 1,
    activity_kind: object = "command",
    status: object = "failed",
    summary: object = "command failed",
    detail: object = None,
) -> dict[str, Any]:
    return {
        "kind": "activity",
        "id": activity_id,
        "revision": revision,
        "activityKind": activity_kind,
        "status": status,
        "summary": summary,
        "detail": {} if detail is None else detail,
    }


def capture_snapshot(snapshot: dict[str, Any]) -> Observation:
    class SnapshotClient:
        def get_session(self, session_id: str) -> dict[str, Any]:
            return session()

        def get_conversation(
            self, session_id: str, *, limit: int
        ) -> dict[str, Any]:
            assert limit == 500
            return deepcopy(snapshot)

        def get_workspace_summary(self, session_id: str) -> dict[str, Any]:
            return workspace()

    return capture_observation(  # type: ignore[arg-type]
        SnapshotClient(),
        WORKER_ID,
    )


def test_capture_uses_only_three_public_get_routes_and_saves_required_state(
    tmp_path: Path,
) -> None:
    seen: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.path == f"/api/v1/sessions/{WORKER_ID}":
            return httpx.Response(200, json={"session": session()})
        if request.url.path.endswith("/conversation"):
            return httpx.Response(200, json=conversation())
        if request.url.path.endswith("/workspace/files"):
            return httpx.Response(200, json=workspace())
        raise AssertionError(f"unexpected route: {request.method} {request.url}")

    with AOClient(
        write_runfile(tmp_path / "running.json"),
        transport=httpx.MockTransport(handler),
    ) as client:
        captured = capture_observation(client, WORKER_ID)

    assert seen == [
        ("GET", f"/api/v1/sessions/{WORKER_ID}", {}),
        ("GET", f"/api/v1/sessions/{WORKER_ID}/conversation", {"limit": "500"}),
        ("GET", f"/api/v1/sessions/{WORKER_ID}/workspace/files", {}),
    ]
    assert captured.worker_session_id == WORKER_ID
    assert captured.project_id == PROJECT_ID
    assert captured.activity_state == "active"
    assert captured.latest_sequence == 9
    assert captured.turns == (
        TurnObservation("turn-1", "completed"),
        TurnObservation("turn-2", "failed"),
    )
    assert captured.messages == (MessageObservation("message-1", 2, False),)
    assert captured.activities == (
        ActivityObservation("activity-1", 3, "command", "completed"),
    )
    assert captured.workspace_files == (
        WorkspaceFileObservation("src/example.py", "modified"),
    )
    assert captured.commit_shas == ("abc123",)
    assert captured.failures == (
        FailureOccurrence("turn:turn-2", "provider failed"),
    )


def test_two_failed_command_activities_trigger_repeated_failure() -> None:
    old_activity = activity_item(
        "command-1",
        detail={"output": "  PYTEST\n FAILED  "},
    )
    new_activity = activity_item(
        "command-2",
        detail={"output": "pytest failed"},
    )
    previous_snapshot = conversation()
    previous_snapshot["turns"] = [{"id": "turn-1", "state": "completed"}]
    previous_snapshot["activities"] = [old_activity]
    current_snapshot = deepcopy(previous_snapshot)
    current_snapshot["activities"] = [old_activity, new_activity]

    result = evaluate_observation(
        capture_snapshot(previous_snapshot),
        capture_snapshot(current_snapshot),
        0,
    )

    assert result is not None
    assert result.trigger is ObserverTrigger.REPEATED_FAILURE
    assert "failure fingerprint: pytest failed" in result.evidence
    assert "new source ids: activity:command-2" in result.evidence


def test_completed_turn_with_failed_pytest_activity_captures_failure() -> None:
    snapshot = conversation()
    snapshot["turns"] = [{"id": "turn-1", "state": "completed"}]
    snapshot["activities"] = [
        activity_item(
            "pytest-command",
            summary="pytest command",
            detail={"output": "2 FAILED"},
        )
    ]

    captured = capture_snapshot(snapshot)

    assert captured.failures == (
        FailureOccurrence("activity:pytest-command", "2 failed"),
    )


def test_turn_error_message_failure_path_remains_available() -> None:
    snapshot = conversation()
    snapshot["activities"] = []

    captured = capture_snapshot(snapshot)

    assert captured.failures == (
        FailureOccurrence("turn:turn-2", "provider failed"),
    )


def test_turn_and_activity_failure_sources_use_distinct_prefixes() -> None:
    snapshot = conversation()
    snapshot["turns"] = [
        {"id": "same-id", "state": "failed", "errorMessage": "same failure"}
    ]
    snapshot["activities"] = [
        activity_item("same-id", detail={"output": "same failure"})
    ]

    captured = capture_snapshot(snapshot)

    assert captured.failures == (
        FailureOccurrence("activity:same-id", "same failure"),
        FailureOccurrence("turn:same-id", "same failure"),
    )


def test_existing_failed_activity_does_not_trigger_without_new_source() -> None:
    snapshot = conversation()
    snapshot["turns"] = [{"id": "turn-1", "state": "completed"}]
    snapshot["activities"] = [
        activity_item("command-1", detail={"output": "pytest failed"}),
        activity_item("command-2", detail={"output": "pytest failed"}),
    ]
    previous = capture_snapshot(snapshot)
    current = capture_snapshot(snapshot)

    assert evaluate_observation(previous, current, 0) is None


@pytest.mark.parametrize(
    ("detail", "summary", "expected"),
    [
        ({"output": " output text ", "error": "error text"}, "summary", "output text"),
        ({"output": "  ", "error": " Error Text "}, "summary", "error text"),
        ({}, " Summary Text ", "summary text"),
    ],
)
def test_activity_failure_text_uses_output_error_summary_order(
    detail: dict[str, object],
    summary: str,
    expected: str,
) -> None:
    snapshot = conversation()
    snapshot["turns"] = [{"id": "turn-1", "state": "completed"}]
    snapshot["activities"] = [
        activity_item(
            "error-1",
            activity_kind="error",
            status="completed",
            summary=summary,
            detail=detail,
        )
    ]

    captured = capture_snapshot(snapshot)

    assert captured.failures == (
        FailureOccurrence("activity:error-1", expected),
    )


def test_turn_completing_in_place_is_a_milestone() -> None:
    previous = observation(activity="active")
    current = observation(
        activity="idle",
        turns=(TurnObservation("turn-1", "completed"),),
    )

    result = evaluate_observation(previous, current, 1)

    assert result is not None
    assert result.trigger is ObserverTrigger.MILESTONE
    assert "turn-1" in result.evidence[0]


@pytest.mark.parametrize("activity", ["idle", "waiting_input"])
def test_new_completed_turn_in_safe_idle_activity_is_a_milestone(
    activity: str,
) -> None:
    previous = observation(turns=())
    current = observation(
        activity=activity,
        turns=(TurnObservation("turn-new", "completed"),),
    )

    result = evaluate_observation(previous, current, 0)

    assert result is not None
    assert result.trigger is ObserverTrigger.MILESTONE


def test_message_revision_change_resets_stall_even_if_sequence_is_unchanged() -> None:
    previous = observation()
    current = observation(messages=(MessageObservation("message-1", 2, True),))

    assert (
        evaluate_observation(
            previous,
            current,
            600,
            stall_threshold_seconds=300,
        )
        is None
    )


def test_activity_revision_change_resets_stall() -> None:
    previous = observation()
    current = observation(
        activities=(ActivityObservation("activity-1", 2, "command", "running"),)
    )

    assert evaluate_observation(previous, current, 300) is None


def test_activity_status_change_resets_stall() -> None:
    previous = observation()
    current = observation(
        activities=(
            ActivityObservation("activity-1", 1, "command", "completed"),
        )
    )

    assert evaluate_observation(previous, current, 300) is None


def test_idle_to_active_transition_does_not_trigger_stall() -> None:
    previous = observation(activity="idle")
    current = observation(activity="active")

    assert evaluate_observation(previous, current, 300) is None


@pytest.mark.parametrize(
    ("files", "commits"),
    [
        ((WorkspaceFileObservation("src/new.py", "untracked"),), ()),
        ((), ("new-sha",)),
    ],
)
def test_workspace_or_commit_change_resets_stall(
    files: tuple[WorkspaceFileObservation, ...],
    commits: tuple[str, ...],
) -> None:
    previous = observation()
    current = observation(files=files, commits=commits)

    assert evaluate_observation(previous, current, 300) is None


def test_old_and_new_same_failure_reach_repeated_failure_threshold() -> None:
    old = FailureOccurrence("turn:turn-old", "provider failed")
    new = FailureOccurrence("turn:turn-new", "provider failed")
    previous = observation(failures=(old,))
    current = observation(failures=(old, new))

    result = evaluate_observation(previous, current, 0)

    assert result is not None
    assert result.trigger is ObserverTrigger.REPEATED_FAILURE
    assert "occurrence count: 2" in result.evidence
    assert "new source ids: turn:turn-new" in result.evidence


def test_previous_failure_counts_when_current_only_has_new_occurrence() -> None:
    previous = observation(
        failures=(FailureOccurrence("turn:turn-old", "provider failed"),)
    )
    current = observation(
        failures=(FailureOccurrence("turn:turn-new", "provider failed"),)
    )

    result = evaluate_observation(previous, current, 0)

    assert result is not None
    assert result.trigger is ObserverTrigger.REPEATED_FAILURE


def test_existing_failures_do_not_trigger_repeatedly_without_new_occurrence() -> None:
    failures = (
        FailureOccurrence("turn:turn-1", "provider failed"),
        FailureOccurrence("turn:turn-2", "provider failed"),
    )
    previous = observation(failures=failures)
    current = observation(failures=failures)

    assert evaluate_observation(previous, current, 0) is None


def test_repeated_failure_has_priority_over_milestone() -> None:
    old = FailureOccurrence("turn:turn-old", "provider failed")
    previous = observation(activity="active", failures=(old,))
    current = observation(
        activity="idle",
        turns=(TurnObservation("turn-1", "completed"),),
        failures=(old, FailureOccurrence("turn:turn-new", "provider failed")),
    )

    result = evaluate_observation(previous, current, 300)

    assert result is not None
    assert result.trigger is ObserverTrigger.REPEATED_FAILURE


def test_active_unchanged_progress_signature_reaching_threshold_is_stall() -> None:
    previous = observation(latest_sequence=1)
    current = observation(latest_sequence=99)

    result = evaluate_observation(
        previous,
        current,
        300,
        stall_threshold_seconds=300,
    )

    assert result is not None
    assert result.trigger is ObserverTrigger.STALL
    assert "progress signature is unchanged" in result.evidence


def test_stall_evidence_is_stable_after_threshold_is_reached() -> None:
    previous = observation(latest_sequence=1)
    current = observation(latest_sequence=99)

    at_threshold = evaluate_observation(
        previous,
        current,
        300,
        stall_threshold_seconds=300,
    )
    after_threshold = evaluate_observation(
        previous,
        current,
        301,
        stall_threshold_seconds=300,
    )

    assert at_threshold is not None
    assert after_threshold is not None
    assert at_threshold.evidence == after_threshold.evidence == (
        "progress signature is unchanged",
        "progress remained unchanged for at least the stall threshold",
        "stall threshold is 300 seconds",
    )


@pytest.mark.parametrize(
    ("previous_failures", "current_failures", "threshold"),
    [
        (
            (FailureOccurrence("turn:turn-1", "first error"),),
            (
                FailureOccurrence("turn:turn-1", "first error"),
                FailureOccurrence("turn:turn-2", "second error"),
            ),
            2,
        ),
        (
            (FailureOccurrence("turn:turn-1", "same error"),),
            (
                FailureOccurrence("turn:turn-1", "same error"),
                FailureOccurrence("turn:turn-2", "same error"),
            ),
            3,
        ),
    ],
)
def test_different_or_below_threshold_failures_do_not_trigger(
    previous_failures: tuple[FailureOccurrence, ...],
    current_failures: tuple[FailureOccurrence, ...],
    threshold: int,
) -> None:
    previous = observation(failures=previous_failures)
    current = observation(failures=current_failures)

    assert (
        evaluate_observation(
            previous,
            current,
            0,
            repeated_failure_threshold=threshold,
        )
        is None
    )


@pytest.mark.parametrize(
    ("activity", "elapsed"),
    [("idle", 300), ("active", 299)],
)
def test_ordinary_unchanged_observation_does_not_trigger(
    activity: str,
    elapsed: float,
) -> None:
    previous = observation(activity=activity)
    current = observation(activity=activity)

    assert evaluate_observation(previous, current, elapsed) is None


def test_failure_fingerprint_uses_only_case_and_whitespace_normalization() -> None:
    assert normalize_failure_fingerprint("  Foo\t BAR\nBaz  ") == "foo bar baz"
    first_path = normalize_failure_fingerprint("error: path-1")
    second_path = normalize_failure_fingerprint("error: path-2")
    assert first_path != second_path


@pytest.mark.parametrize(
    ("session_change", "message"),
    [
        ({"kind": "orchestrator"}, "kind 'worker'"),
        ({"mode": "tui"}, "Chat mode"),
        ({"isTerminated": True, "status": "terminated"}, "not be terminated"),
        ({"activity": {"state": "blocked"}}, "blocked or exited"),
        ({"activity": {"state": "exited"}}, "blocked or exited"),
    ],
)
def test_capture_rejects_non_worker_tui_and_terminated_before_other_reads(
    session_change: dict[str, Any],
    message: str,
) -> None:
    class UnsafeClient:
        def get_session(self, session_id: str) -> dict[str, Any]:
            payload = session()
            payload.update(session_change)
            return payload

        def get_conversation(self, *args: object, **kwargs: object) -> dict[str, Any]:
            raise AssertionError("unsafe session must fail before conversation read")

        def get_workspace_summary(self, session_id: str) -> dict[str, Any]:
            raise AssertionError("unsafe session must fail before workspace read")

    with pytest.raises(ObserverError, match=message):
        capture_observation(UnsafeClient(), WORKER_ID)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("target", "change", "message"),
    [
        ("session", {"activity": "active"}, "activity must be an object"),
        ("conversation", {"sessionId": "other"}, "does not match"),
        ("conversation", {"turns": [{"id": "turn-1"}]}, "turn state"),
        (
            "conversation",
            {
                "messages": [
                    {"id": "message-1", "revision": True, "streaming": False}
                ]
            },
            "revision",
        ),
        (
            "conversation",
            {
                "activities": [
                    activity_item("duplicate"),
                    activity_item("duplicate"),
                ]
            },
            "duplicate activity id",
        ),
        (
            "conversation",
            {"activities": [activity_item("", status="completed")]},
            "activity id",
        ),
        (
            "conversation",
            {
                "activities": [
                    activity_item("activity-1", revision=-1, status="completed")
                ]
            },
            "revision",
        ),
        (
            "conversation",
            {
                "activities": [
                    activity_item(
                        "activity-1",
                        activity_kind="shell",
                        status="completed",
                    )
                ]
            },
            "activityKind",
        ),
        (
            "conversation",
            {
                "activities": [
                    activity_item("activity-1", status="succeeded")
                ]
            },
            "status",
        ),
        ("workspace", {"sessionId": "other"}, "does not match"),
        ("workspace", {"truncated": True}, "not truncated"),
        ("workspace", {"commits": [{"sha": ""}]}, "commit sha"),
    ],
)
def test_capture_rejects_invalid_or_mismatched_responses(
    target: str,
    change: dict[str, Any],
    message: str,
) -> None:
    class InvalidClient:
        def get_session(self, session_id: str) -> dict[str, Any]:
            payload = session()
            if target == "session":
                payload.update(change)
            return payload

        def get_conversation(
            self, session_id: str, *, limit: int
        ) -> dict[str, Any]:
            payload = conversation()
            if target == "conversation":
                payload.update(deepcopy(change))
            return payload

        def get_workspace_summary(self, session_id: str) -> dict[str, Any]:
            payload = workspace()
            if target == "workspace":
                payload.update(deepcopy(change))
            return payload

    with pytest.raises(ObserverError, match=message):
        capture_observation(InvalidClient(), WORKER_ID)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("elapsed", "failure_threshold", "stall_threshold"),
    [(-1, 2, 300), (0, 0, 300), (0, 2, -1)],
)
def test_evaluation_threshold_inputs_fail_closed(
    elapsed: float,
    failure_threshold: int,
    stall_threshold: float,
) -> None:
    with pytest.raises(ValueError):
        evaluate_observation(
            observation(),
            observation(),
            elapsed,
            repeated_failure_threshold=failure_threshold,
            stall_threshold_seconds=stall_threshold,
        )
