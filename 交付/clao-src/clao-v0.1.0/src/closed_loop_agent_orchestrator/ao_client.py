"""Synchronous client for the local AO daemon."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx


class AOClientError(RuntimeError):
    """Base error raised by the AO client."""


class AODiscoveryError(AOClientError):
    """The AO daemon runfile could not be loaded or validated."""


class AORequestError(AOClientError):
    """An AO request failed in transport or returned a non-success status."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        ao_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.message = ao_message or message


class AOResponseError(AOClientError):
    """An AO success response did not have the expected minimal shape."""


class AOConversationTimeoutError(AOClientError):
    """A conversation did not advance before the polling deadline."""


class AOClient:
    """Access the same-machine AO daemon over its loopback REST API."""

    def __init__(
        self,
        run_file: str | os.PathLike[str] | None = None,
        *,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.run_file = _resolve_run_file(run_file)
        port = _read_daemon_port(self.run_file)
        self.base_url = f"http://127.0.0.1:{port}"
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            trust_env=False,
        )

    def __enter__(self) -> AOClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""

        self._client.close()

    def check_health(self) -> dict[str, Any]:
        """Return the minimally validated health response."""

        payload = self._request_json_object("/healthz")
        status = payload.get("status")
        if not isinstance(status, str) or not status:
            raise AOResponseError("AO health response is missing a non-empty status")
        return payload

    def get_openapi(self) -> str:
        """Return the daemon's OpenAPI document after a non-empty check."""

        response = self._request("/api/v1/openapi.yaml")
        document = response.text
        if not document.strip():
            raise AOResponseError("AO OpenAPI response is empty")
        return document

    def list_projects(self) -> list[dict[str, Any]]:
        """List AO projects."""

        payload = self._request_json_object("/api/v1/projects")
        return _dict_list(payload, "projects", "AO projects response")

    def get_project(self, project_id: str) -> dict[str, Any]:
        """Get one AO project by ID."""

        path = f"/api/v1/projects/{_path_id(project_id, 'project_id')}"
        payload = self._request_json_object(path)
        return _dict_value(payload, "project", "AO project response")

    def list_sessions(
        self,
        *,
        project: str | None = None,
        active: bool | None = None,
    ) -> list[dict[str, Any]]:
        """List AO sessions, optionally filtered by project and active state."""

        params: dict[str, str] = {}
        if project is not None:
            if not isinstance(project, str) or not project:
                raise ValueError("project must be a non-empty string")
            params["project"] = project
        if active is not None:
            if not isinstance(active, bool):
                raise ValueError("active must be a bool")
            params["active"] = str(active).lower()

        payload = self._request_json_object("/api/v1/sessions", params=params)
        return _dict_list(payload, "sessions", "AO sessions response")

    def get_session(self, session_id: str) -> dict[str, Any]:
        """Get one AO session by ID."""

        path = f"/api/v1/sessions/{_path_id(session_id, 'session_id')}"
        payload = self._request_json_object(path)
        return _dict_value(payload, "session", "AO session response")

    def get_workspace_summary(self, session_id: str) -> dict[str, Any]:
        """Get the public changed-file and commit summary for one workspace."""

        path = (
            f"/api/v1/sessions/{_path_id(session_id, 'session_id')}"
            "/workspace/files"
        )
        payload = self._request_json_object(path)
        _validate_workspace_summary(payload)
        return payload

    def get_conversation(
        self,
        session_id: str,
        before_sequence: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get a minimally validated AO Conversation Snapshot."""

        params: dict[str, str] = {}
        if before_sequence is not None:
            _require_int_range(before_sequence, "before_sequence", minimum=1)
            params["beforeSequence"] = str(before_sequence)
        if limit is not None:
            _require_int_range(limit, "limit", minimum=1, maximum=500)
            params["limit"] = str(limit)

        path = (
            f"/api/v1/sessions/{_path_id(session_id, 'session_id')}/conversation"
        )
        payload = self._request_json_object(path, params=params)
        _validate_conversation_snapshot(payload)
        return payload

    def send_conversation_message(
        self,
        session_id: str,
        text: str,
        client_message_id: str,
    ) -> dict[str, Any]:
        """Send one idempotently identified message to an AO Chat session."""

        if not isinstance(text, str) or not text:
            raise ValueError("text must be a non-empty string")
        if not isinstance(client_message_id, str) or not client_message_id:
            raise ValueError("client_message_id must be a non-empty string")

        path = (
            f"/api/v1/sessions/{_path_id(session_id, 'session_id')}"
            "/conversation/messages"
        )
        payload = self._request_json_object(
            path,
            method="POST",
            json_body={"text": text, "clientMessageId": client_message_id},
        )
        _validate_send_conversation_response(payload)
        return payload

    def wait_for_conversation_update(
        self,
        session_id: str,
        after_sequence: int,
        poll_interval: float = 2,
        timeout: float = 90,
    ) -> dict[str, Any]:
        """Poll until the Conversation Snapshot's latest sequence advances."""

        _require_int_range(after_sequence, "after_sequence", minimum=0)
        _require_positive_number(poll_interval, "poll_interval")
        _require_positive_number(timeout, "timeout", allow_zero=True)

        deadline = time.monotonic() + timeout
        while True:
            snapshot = self.get_conversation(session_id)
            if snapshot["latestSequence"] > after_sequence:
                return snapshot

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AOConversationTimeoutError(
                    "AO conversation did not advance beyond "
                    f"sequence {after_sequence} within {timeout:g} seconds "
                    f"for session {session_id!r}"
                )
            time.sleep(min(poll_interval, remaining))

    def _request_json_object(
        self,
        path: str,
        *,
        method: str = "GET",
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._request(
            path,
            method=method,
            params=params,
            json_body=json_body,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AOResponseError(
                f"AO response is not valid JSON: {method} {path}"
            ) from exc
        if not isinstance(payload, dict):
            raise AOResponseError(
                f"AO response is not a JSON object: {method} {path}"
            )
        return payload

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = self._client.request(
                method,
                path,
                params=params,
                json=json_body,
            )
        except httpx.TimeoutException as exc:
            raise AORequestError(
                f"AO request timed out: {method} {path}"
            ) from exc
        except httpx.RequestError as exc:
            raise AORequestError(
                f"AO request failed: {method} {path} ({type(exc).__name__})"
            ) from exc

        if not 200 <= response.status_code < 300:
            raise _http_error(response, method, path)
        return response


def _resolve_run_file(
    explicit: str | os.PathLike[str] | None,
) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    if "AO_RUN_FILE" in os.environ:
        configured = os.environ["AO_RUN_FILE"]
        if not configured:
            raise AODiscoveryError("AO_RUN_FILE is set but empty")
        return Path(configured).expanduser()
    return Path.home() / ".ao" / "running.json"


def _read_daemon_port(run_file: Path) -> int:
    try:
        with run_file.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise AODiscoveryError("AO runfile does not exist") from exc
    except (OSError, UnicodeError) as exc:
        raise AODiscoveryError(
            f"AO runfile could not be read ({type(exc).__name__})"
        ) from exc
    except json.JSONDecodeError as exc:
        raise AODiscoveryError("AO runfile contains invalid JSON") from exc

    if not isinstance(payload, dict):
        raise AODiscoveryError("AO runfile JSON must be an object")

    pid = payload.get("pid")
    port = payload.get("port")
    if not _is_int(pid) or pid <= 0:
        raise AODiscoveryError("AO runfile pid must be a positive integer")
    if not _is_int(port) or not 1 <= port <= 65535:
        raise AODiscoveryError("AO runfile port must be an integer from 1 to 65535")
    return port


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int_range(
    value: object,
    name: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> None:
    if not _is_int(value) or value < minimum or (
        maximum is not None and value > maximum
    ):
        if maximum is None:
            raise ValueError(f"{name} must be an integer >= {minimum}")
        raise ValueError(
            f"{name} must be an integer from {minimum} to {maximum}"
        )


def _require_positive_number(
    value: object,
    name: str,
    *,
    allow_zero: bool = False,
) -> None:
    valid_type = isinstance(value, (int, float)) and not isinstance(value, bool)
    valid_value = valid_type and (value >= 0 if allow_zero else value > 0)
    if not valid_value:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a {qualifier} number")


def _path_id(value: str, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return quote(value, safe="")


def _dict_list(
    payload: Mapping[str, Any],
    key: str,
    description: str,
) -> list[dict[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise AOResponseError(
            f"{description} must contain a list of objects in {key!r}"
        )
    return value


def _dict_value(
    payload: Mapping[str, Any],
    key: str,
    description: str,
) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise AOResponseError(f"{description} must contain an object in {key!r}")
    return value


def _validate_conversation_snapshot(payload: Mapping[str, Any]) -> None:
    _dict_list(payload, "turns", "AO conversation snapshot")
    _dict_list(payload, "messages", "AO conversation snapshot")
    _dict_list(payload, "activities", "AO conversation snapshot")

    latest_sequence = payload.get("latestSequence")
    if not _is_int(latest_sequence) or latest_sequence < 0:
        raise AOResponseError(
            "AO conversation snapshot latestSequence must be a non-negative integer"
        )

    if "oldestSequence" in payload:
        oldest_sequence = payload["oldestSequence"]
        if not _is_int(oldest_sequence) or oldest_sequence < 0:
            raise AOResponseError(
                "AO conversation snapshot oldestSequence must be a non-negative integer"
            )

    if not isinstance(payload.get("hasMoreBefore"), bool):
        raise AOResponseError(
            "AO conversation snapshot hasMoreBefore must be a boolean"
        )


def _validate_workspace_summary(payload: Mapping[str, Any]) -> None:
    session_id = payload.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise AOResponseError(
            "AO workspace summary sessionId must be a non-empty string"
        )
    _dict_list(payload, "files", "AO workspace summary")
    _dict_list(payload, "commits", "AO workspace summary")
    if not isinstance(payload.get("truncated"), bool):
        raise AOResponseError("AO workspace summary truncated must be a boolean")


_TURN_STATES = frozenset(
    {"queued", "running", "completed", "recovered", "interrupted", "failed"}
)


def _validate_send_conversation_response(payload: Mapping[str, Any]) -> None:
    duplicate = payload.get("duplicate")
    if not isinstance(duplicate, bool):
        raise AOResponseError(
            "AO conversation message response duplicate must be a boolean"
        )

    for key in ("turnId", "providerTurnId", "state"):
        value = payload.get(key)
        if value is not None and not isinstance(value, str):
            raise AOResponseError(
                f"AO conversation message response {key} must be a string"
            )

    turn_id = payload.get("turnId", "")
    provider_turn_id = payload.get("providerTurnId", "")
    state = payload.get("state", "")
    if state and state not in _TURN_STATES:
        raise AOResponseError(
            f"AO conversation message response has invalid state {state!r}"
        )
    if not duplicate and (not turn_id or not state):
        raise AOResponseError(
            "AO conversation message response requires non-empty turnId, "
            "and state when duplicate is false"
        )
    if not duplicate and state != "queued" and not provider_turn_id:
        raise AOResponseError(
            "AO conversation message response requires a non-empty "
            "providerTurnId unless state is queued"
        )


def _http_error(
    response: httpx.Response,
    method: str,
    path: str,
) -> AORequestError:
    code: str | None = None
    message: str | None = None
    request_id: str | None = None
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        code = _optional_string(payload.get("code"))
        message = _optional_string(payload.get("message"))
        request_id = _optional_string(payload.get("requestId"))
        if message is None:
            message = _optional_string(payload.get("error"))

    request_id = request_id or _optional_string(response.headers.get("x-request-id"))
    details = [f"AO returned HTTP {response.status_code}: {method} {path}"]
    if code:
        details.append(f"code={code}")
    if message:
        details.append(f"message={message}")
    if request_id:
        details.append(f"requestId={request_id}")
    return AORequestError(
        "; ".join(details),
        status_code=response.status_code,
        code=code,
        request_id=request_id,
        ao_message=message,
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
