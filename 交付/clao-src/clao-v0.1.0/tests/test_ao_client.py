from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import closed_loop_agent_orchestrator.ao_client as ao_client_module
from closed_loop_agent_orchestrator.ao_client import (
    AOClient,
    AOConversationTimeoutError,
    AODiscoveryError,
    AORequestError,
    AOResponseError,
)


def write_runfile(path: Path, *, pid: object = 123, port: object = 4567) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "port": port}), encoding="utf-8")
    return path


def conversation_snapshot(latest_sequence: object) -> dict[str, object]:
    return {
        "turns": [],
        "messages": [],
        "activities": [],
        "latestSequence": latest_sequence,
        "hasMoreBefore": False,
    }


def workspace_summary() -> dict[str, object]:
    return {
        "sessionId": "session/one",
        "files": [],
        "commits": [],
        "truncated": False,
    }


def test_explicit_runfile_takes_priority_over_environment_and_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = write_runfile(tmp_path / "explicit.json", port=4101)
    environment = write_runfile(tmp_path / "environment.json", port=4102)
    write_runfile(tmp_path / "home" / ".ao" / "running.json", port=4103)
    monkeypatch.setenv("AO_RUN_FILE", str(environment))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    with AOClient(explicit, transport=httpx.MockTransport(lambda request: None)) as client:
        assert client.run_file == explicit
        assert client.base_url == "http://127.0.0.1:4101"


def test_environment_runfile_takes_priority_over_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = write_runfile(tmp_path / "environment.json", port=4201)
    write_runfile(tmp_path / "home" / ".ao" / "running.json", port=4202)
    monkeypatch.setenv("AO_RUN_FILE", str(environment))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    with AOClient(transport=httpx.MockTransport(lambda request: None)) as client:
        assert client.run_file == environment
        assert client.base_url == "http://127.0.0.1:4201"


def test_default_runfile_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    default = write_runfile(tmp_path / ".ao" / "running.json", port=4301)
    monkeypatch.delenv("AO_RUN_FILE", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    with AOClient(transport=httpx.MockTransport(lambda request: None)) as client:
        assert client.run_file == default
        assert client.base_url == "http://127.0.0.1:4301"


def test_empty_environment_runfile_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AO_RUN_FILE", "")

    with pytest.raises(AODiscoveryError, match="set but empty"):
        AOClient()


def test_missing_runfile_has_clear_error(tmp_path: Path) -> None:
    with pytest.raises(AODiscoveryError, match="does not exist"):
        AOClient(tmp_path / "missing.json")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not json", "invalid JSON"),
        ("[]", "must be an object"),
    ],
)
def test_malformed_runfile_has_clear_error(
    tmp_path: Path, content: str, message: str
) -> None:
    runfile = tmp_path / "running.json"
    runfile.write_text(content, encoding="utf-8")

    with pytest.raises(AODiscoveryError, match=message):
        AOClient(runfile)


@pytest.mark.parametrize("pid", [None, 0, -1, True, "123"])
def test_runfile_requires_positive_integer_pid(tmp_path: Path, pid: object) -> None:
    runfile = write_runfile(tmp_path / "running.json", pid=pid)

    with pytest.raises(AODiscoveryError, match="positive integer"):
        AOClient(runfile)


@pytest.mark.parametrize("port", [None, 0, -1, 65536, True, "4567"])
def test_runfile_requires_valid_integer_port(tmp_path: Path, port: object) -> None:
    runfile = write_runfile(tmp_path / "running.json", port=port)

    with pytest.raises(AODiscoveryError, match="1 to 65535"):
        AOClient(runfile)


def test_requests_are_fixed_to_loopback_and_health_is_validated(tmp_path: Path) -> None:
    runfile = write_runfile(tmp_path / "running.json", port=4401)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.scheme == "http"
        assert request.url.host == "127.0.0.1"
        assert request.url.port == 4401
        assert request.url.path == "/healthz"
        return httpx.Response(200, json={"status": "ok"})

    with AOClient(runfile, transport=httpx.MockTransport(handler)) as client:
        assert client.check_health() == {"status": "ok"}


def test_health_rejects_missing_status(tmp_path: Path) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))

    with AOClient(runfile, transport=transport) as client:
        with pytest.raises(AOResponseError, match="health response"):
            client.check_health()


def test_openapi_uses_expected_method_and_path(tmp_path: Path) -> None:
    runfile = write_runfile(tmp_path / "running.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/openapi.yaml"
        return httpx.Response(200, text="openapi: 3.0.3\n")

    with AOClient(runfile, transport=httpx.MockTransport(handler)) as client:
        assert client.get_openapi() == "openapi: 3.0.3\n"


def test_openapi_rejects_empty_response(tmp_path: Path) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="  \n"))

    with AOClient(runfile, transport=transport) as client:
        with pytest.raises(AOResponseError, match="OpenAPI response is empty"):
            client.get_openapi()


def test_ao_error_envelope_is_preserved(tmp_path: Path) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    response_body = {
        "error": "not_found",
        "code": "PROJECT_NOT_FOUND",
        "message": "project does not exist",
        "requestId": "request-123",
    }
    transport = httpx.MockTransport(
        lambda request: httpx.Response(404, json=response_body)
    )

    with AOClient(runfile, transport=transport) as client:
        with pytest.raises(AORequestError) as raised:
            client.get_project("missing")

    error = raised.value
    assert error.status_code == 404
    assert error.code == "PROJECT_NOT_FOUND"
    assert error.request_id == "request-123"
    assert error.message == "project does not exist"
    assert "project does not exist" in str(error)
    assert "requestId=request-123" in str(error)


def test_connection_error_is_clear(tmp_path: Path) -> None:
    runfile = write_runfile(tmp_path / "running.json")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with AOClient(runfile, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AORequestError, match="request failed.*ConnectError"):
            client.check_health()


def test_timeout_error_is_clear(tmp_path: Path) -> None:
    runfile = write_runfile(tmp_path / "running.json")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with AOClient(runfile, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AORequestError, match="request timed out"):
            client.check_health()


def test_project_routes_use_get_and_unwrap_responses(tmp_path: Path) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/v1/projects":
            return httpx.Response(200, json={"projects": [{"id": "project-1"}]})
        if request.url.path == "/api/v1/projects/project-1":
            return httpx.Response(200, json={"project": {"id": "project-1"}})
        raise AssertionError(f"unexpected path: {request.url.path}")

    with AOClient(runfile, transport=httpx.MockTransport(handler)) as client:
        assert client.list_projects() == [{"id": "project-1"}]
        assert client.get_project("project-1") == {"id": "project-1"}

    assert seen == [
        ("GET", "/api/v1/projects"),
        ("GET", "/api/v1/projects/project-1"),
    ]


@pytest.mark.parametrize(
    ("payload", "method"),
    [
        ({"projects": {}}, "list_projects"),
        ({"projects": ["not-an-object"]}, "list_projects"),
        ({"project": []}, "get_project"),
    ],
)
def test_project_responses_receive_minimal_shape_validation(
    tmp_path: Path, payload: object, method: str
) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))

    with AOClient(runfile, transport=transport) as client:
        with pytest.raises(AOResponseError):
            if method == "list_projects":
                client.list_projects()
            else:
                client.get_project("project-1")


def test_session_routes_and_filters_use_get(tmp_path: Path) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    seen: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.path == "/api/v1/sessions":
            return httpx.Response(200, json={"sessions": [{"id": "session-1"}]})
        if request.url.path == "/api/v1/sessions/session-1":
            return httpx.Response(200, json={"session": {"id": "session-1"}})
        raise AssertionError(f"unexpected path: {request.url.path}")

    with AOClient(runfile, transport=httpx.MockTransport(handler)) as client:
        assert client.list_sessions(project="project-1", active=False) == [
            {"id": "session-1"}
        ]
        assert client.get_session("session-1") == {"id": "session-1"}

    assert seen == [
        (
            "GET",
            "/api/v1/sessions",
            {"project": "project-1", "active": "false"},
        ),
        ("GET", "/api/v1/sessions/session-1", {}),
    ]


def test_session_list_omits_unspecified_filters(tmp_path: Path) -> None:
    runfile = write_runfile(tmp_path / "running.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/sessions"
        assert not request.url.query
        return httpx.Response(200, json={"sessions": []})

    with AOClient(runfile, transport=httpx.MockTransport(handler)) as client:
        assert client.list_sessions() == []


def test_ids_are_escaped_as_single_path_segments(tmp_path: Path) -> None:
    runfile = write_runfile(tmp_path / "running.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == b"/api/v1/sessions/session%2Fone"
        return httpx.Response(200, json={"session": {"id": "session/one"}})

    with AOClient(runfile, transport=httpx.MockTransport(handler)) as client:
        assert client.get_session("session/one") == {"id": "session/one"}


def test_workspace_summary_uses_public_read_only_route(tmp_path: Path) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    expected = workspace_summary()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.raw_path == (
            b"/api/v1/sessions/session%2Fone/workspace/files"
        )
        return httpx.Response(200, json=expected)

    with AOClient(runfile, transport=httpx.MockTransport(handler)) as client:
        assert client.get_workspace_summary("session/one") == expected


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"files": [], "commits": [], "truncated": False}, "sessionId"),
        ({**workspace_summary(), "files": ["path"]}, "files"),
        ({**workspace_summary(), "commits": ["sha"]}, "commits"),
        ({**workspace_summary(), "truncated": "false"}, "truncated"),
    ],
)
def test_workspace_summary_receives_minimal_structure_validation(
    tmp_path: Path, payload: dict[str, object], message: str
) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)
    )

    with AOClient(runfile, transport=transport) as client:
        with pytest.raises(AOResponseError, match=message):
            client.get_workspace_summary("session-1")


def test_non_json_success_response_is_rejected(tmp_path: Path) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="nope"))

    with AOClient(runfile, transport=transport) as client:
        with pytest.raises(AOResponseError, match="not valid JSON"):
            client.list_projects()


def test_conversation_get_uses_expected_method_path_and_query(tmp_path: Path) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    expected = conversation_snapshot(27)
    expected["oldestSequence"] = 12

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.raw_path.split(b"?")[0] == (
            b"/api/v1/sessions/session%2Fone/conversation"
        )
        assert dict(request.url.params) == {
            "beforeSequence": "28",
            "limit": "50",
        }
        return httpx.Response(200, json=expected)

    with AOClient(runfile, transport=httpx.MockTransport(handler)) as client:
        assert client.get_conversation(
            "session/one", before_sequence=28, limit=50
        ) == expected


def test_conversation_get_omits_unspecified_query(tmp_path: Path) -> None:
    runfile = write_runfile(tmp_path / "running.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert not request.url.query
        return httpx.Response(200, json=conversation_snapshot(0))

    with AOClient(runfile, transport=httpx.MockTransport(handler)) as client:
        assert client.get_conversation("session-1")["latestSequence"] == 0


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "turns"),
        (
            {
                **conversation_snapshot(1),
                "messages": ["not-an-object"],
            },
            "messages",
        ),
        (conversation_snapshot(True), "latestSequence"),
        (
            {**conversation_snapshot(1), "oldestSequence": -1},
            "oldestSequence",
        ),
        (
            {**conversation_snapshot(1), "hasMoreBefore": "false"},
            "hasMoreBefore",
        ),
    ],
)
def test_conversation_snapshot_receives_minimal_top_level_validation(
    tmp_path: Path, payload: dict[str, object], message: str
) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)
    )

    with AOClient(runfile, transport=transport) as client:
        with pytest.raises(AOResponseError, match=message):
            client.get_conversation("session-1")


@pytest.mark.parametrize(
    ("before_sequence", "limit", "message"),
    [
        (0, None, "before_sequence"),
        (True, None, "before_sequence"),
        (None, 0, "limit"),
        (None, 501, "limit"),
    ],
)
def test_conversation_query_values_are_validated_before_request(
    tmp_path: Path,
    before_sequence: object,
    limit: object,
    message: str,
) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    transport = httpx.MockTransport(
        lambda request: pytest.fail("invalid query must not reach AO")
    )

    with AOClient(runfile, transport=transport) as client:
        with pytest.raises(ValueError, match=message):
            client.get_conversation(  # type: ignore[arg-type]
                "session-1",
                before_sequence=before_sequence,
                limit=limit,
            )


def test_chat_post_uses_expected_method_path_and_body(tmp_path: Path) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    client_message_id = "  审计-id/原样  "
    response_payload = {
        "turnId": "turn-1",
        "providerTurnId": "provider-turn-1",
        "state": "running",
        "duplicate": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == (
            "/api/v1/sessions/session-1/conversation/messages"
        )
        assert json.loads(request.content) == {
            "text": "检查结果",
            "clientMessageId": client_message_id,
        }
        return httpx.Response(202, json=response_payload)

    with AOClient(runfile, transport=httpx.MockTransport(handler)) as client:
        assert client.send_conversation_message(
            "session-1", "检查结果", client_message_id
        ) == response_payload


def test_chat_post_accepts_duplicate_response_without_turn_fields(
    tmp_path: Path,
) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(202, json={"duplicate": True})
    )

    with AOClient(runfile, transport=transport) as client:
        assert client.send_conversation_message(
            "session-1", "same message", "same-client-id"
        ) == {"duplicate": True}


def test_client_message_id_is_not_cached_locally(tmp_path: Path) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    client_message_id = "message-id-owned-by-ao"
    seen_bodies: list[dict[str, object]] = []
    responses = iter(
        [
            {
                "turnId": "turn-1",
                "providerTurnId": "provider-turn-1",
                "state": "running",
                "duplicate": False,
            },
            {"duplicate": True},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(json.loads(request.content))
        return httpx.Response(202, json=next(responses))

    with AOClient(runfile, transport=httpx.MockTransport(handler)) as client:
        first = client.send_conversation_message(
            "session-1", "same text", client_message_id
        )
        duplicate = client.send_conversation_message(
            "session-1", "same text", client_message_id
        )

    assert first["duplicate"] is False
    assert duplicate == {"duplicate": True}
    assert seen_bodies == [
        {"text": "same text", "clientMessageId": client_message_id},
        {"text": "same text", "clientMessageId": client_message_id},
    ]


def test_chat_post_accepts_queued_response_before_provider_turn_exists(
    tmp_path: Path,
) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    queued = {
        "turnId": "turn-queued",
        "state": "queued",
        "duplicate": False,
    }
    transport = httpx.MockTransport(
        lambda request: httpx.Response(202, json=queued)
    )

    with AOClient(runfile, transport=transport) as client:
        assert client.send_conversation_message(
            "session-1", "queued message", "queued-client-id"
        ) == queued


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "duplicate"),
        ({"duplicate": "false"}, "duplicate"),
        (
            {
                "turnId": "turn-1",
                "providerTurnId": "provider-turn-1",
                "state": "unknown",
                "duplicate": False,
            },
            "invalid state",
        ),
        ({"duplicate": False}, "requires non-empty"),
        (
            {
                "turnId": "turn-1",
                "state": "running",
                "duplicate": False,
            },
            "providerTurnId",
        ),
        ({"duplicate": True, "turnId": 1}, "turnId"),
    ],
)
def test_chat_post_response_fields_are_validated(
    tmp_path: Path, payload: dict[str, object], message: str
) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(202, json=payload)
    )

    with AOClient(runfile, transport=transport) as client:
        with pytest.raises(AOResponseError, match=message):
            client.send_conversation_message("session-1", "text", "message-1")


def test_chat_post_preserves_ao_non_2xx_error(tmp_path: Path) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            409,
            json={
                "code": "CHAT_TURN_ACTIVE",
                "message": "turn already active",
                "requestId": "request-chat-1",
            },
        )
    )

    with AOClient(runfile, transport=transport) as client:
        with pytest.raises(AORequestError) as raised:
            client.send_conversation_message("session-1", "text", "message-1")

    error = raised.value
    assert error.status_code == 409
    assert error.code == "CHAT_TURN_ACTIVE"
    assert error.request_id == "request-chat-1"
    assert "POST /api/v1/sessions/session-1/conversation/messages" in str(error)


def test_wait_for_conversation_update_succeeds_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=conversation_snapshot(11))

    monkeypatch.setattr(
        ao_client_module.time,
        "sleep",
        lambda seconds: pytest.fail("an immediate update must not sleep"),
    )
    with AOClient(runfile, transport=httpx.MockTransport(handler)) as client:
        snapshot = client.wait_for_conversation_update("session-1", 10)

    assert snapshot["latestSequence"] == 11
    assert calls == 1


def test_wait_for_conversation_update_polls_until_sequence_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    sequences = iter([10, 10, 12])
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=conversation_snapshot(next(sequences)))

    monkeypatch.setattr(ao_client_module.time, "sleep", sleeps.append)
    with AOClient(runfile, transport=httpx.MockTransport(handler)) as client:
        snapshot = client.wait_for_conversation_update(
            "session-1", 10, poll_interval=0.25, timeout=5
        )

    assert snapshot["latestSequence"] == 12
    assert sleeps == [0.25, 0.25]


def test_wait_for_conversation_update_times_out_using_monotonic_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    monotonic_values = iter([100.0, 100.2, 100.5])
    sleeps: list[float] = []
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=conversation_snapshot(10))
    )
    monkeypatch.setattr(
        ao_client_module.time, "monotonic", lambda: next(monotonic_values)
    )
    monkeypatch.setattr(ao_client_module.time, "sleep", sleeps.append)

    with AOClient(runfile, transport=transport) as client:
        with pytest.raises(
            AOConversationTimeoutError,
            match="did not advance beyond sequence 10.*0.5 seconds",
        ):
            client.wait_for_conversation_update(
                "session-1", 10, poll_interval=0.25, timeout=0.5
            )

    assert sleeps == [0.25]


def test_wait_for_conversation_update_does_not_retry_ao_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runfile = write_runfile(tmp_path / "running.json")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"message": "daemon unavailable"})

    monkeypatch.setattr(
        ao_client_module.time,
        "sleep",
        lambda seconds: pytest.fail("AO errors must be raised without retrying"),
    )
    with AOClient(runfile, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AORequestError, match="daemon unavailable"):
            client.wait_for_conversation_update("session-1", 10)

    assert calls == 1
