"""Offline tests for the shared Codex CLI structured-output boundary."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopcore.codex_cli import CodexCliError, run_codex_json


def _schema(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text(
        '{"type":"object","properties":{"answer":{"type":"string"}},'
        '"additionalProperties":true}', encoding="utf-8")
    return path


def test_success_uses_safe_command_stdin_and_cleans_output(monkeypatch,
                                                           tmp_path):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        schema_path = command[command.index("--output-schema") + 1]
        seen["schema_path"] = schema_path
        seen["transport_schema"] = json.loads(
            Path(schema_path).read_text(encoding="utf-8"))
        output_path = command[command.index("--output-last-message") + 1]
        seen["output_path"] = output_path
        with open(output_path, "w", encoding="utf-8") as stream:
            json.dump({"ok": True}, stream)
        return SimpleNamespace(returncode=0, stdout="terminal log",
                               stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_codex_json(
        "secret task prompt",
        _schema(tmp_path),
        model="model-x",
        timeout=17,
        codex_bin="codex-test-bin",
        cwd=tmp_path,
    )

    assert result == {"ok": True}
    command = seen["command"]
    assert command[0:2] == ["codex-test-bin", "exec"]
    assert "--skip-git-repo-check" in command
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--model") + 1] == "model-x"
    assert Path(command[command.index("--output-schema") + 1]).name == \
        "schema.json"
    assert seen["transport_schema"]["additionalProperties"] is False
    assert seen["transport_schema"]["required"] == ["answer"]
    assert command[-1] == "-"
    assert "secret task prompt" not in command
    assert seen["kwargs"]["input"] == "secret task prompt"
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["cwd"] == str(tmp_path.resolve())
    assert not Path(seen["output_path"]).exists()
    assert not Path(seen["schema_path"]).exists()


def test_standalone_non_git_cwd_uses_skip_repo_check(monkeypatch, tmp_path):
    standalone = tmp_path / "standalone-clao"
    standalone.mkdir()
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        schema_path = Path(
            command[command.index("--output-schema") + 1])
        seen["schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
        output_path = Path(
            command[command.index("--output-last-message") + 1])
        output_path.write_text('{"answer":"ok"}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_codex_json(
        "standalone prompt", _schema(tmp_path),
        codex_bin="codex-test-bin", cwd=standalone)

    command = seen["command"]
    assert not (standalone / ".git").exists()
    assert result == {"answer": "ok"}
    assert "--skip-git-repo-check" in command
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[-1] == "-"
    assert "standalone prompt" not in command
    assert seen["kwargs"]["input"] == "standalone prompt"
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["cwd"] == str(standalone.resolve())
    assert Path(command[command.index("--output-schema") + 1]).name == \
        "schema.json"
    assert "--output-last-message" in command


def test_planner_transport_schema_preserves_replan_objective(monkeypatch,
                                                             tmp_path):
    seen = {}
    payload = {
        "replacement_task_spec": {
            "objective": "Use a corrected implementation route",
        },
    }

    def fake_run(command, **kwargs):
        schema_path = Path(
            command[command.index("--output-schema") + 1])
        seen["schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
        output_path = Path(
            command[command.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    schema_path = (Path(__file__).resolve().parents[1] / "schemas" /
                   "planner-action.schema.json")

    result = run_codex_json(
        "planner prompt", schema_path, codex_bin="codex-test-bin",
        cwd=tmp_path)

    replacement = seen["schema"]["properties"]["replacement_task_spec"]
    assert replacement["properties"]["objective"] == {
        "type": "string", "minLength": 1}
    assert replacement["required"] == ["objective"]
    assert replacement["additionalProperties"] is False
    assert result == payload


def test_object_schema_without_properties_fails_before_subprocess(
        monkeypatch, tmp_path):
    schema_path = tmp_path / "bad-schema.json"
    schema_path.write_text('{"type":["object","null"]}', encoding="utf-8")
    called = False

    def fake_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("subprocess must not start")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(CodexCliError,
                       match="object schema must declare properties"):
        run_codex_json("prompt", schema_path,
                       codex_bin="codex-test-bin")
    assert called is False


def test_nonzero_exit_raises_short_error_without_prompt(monkeypatch, tmp_path):
    seen = {}

    def fake_run(command, **kwargs):
        seen["output_path"] = command[
            command.index("--output-last-message") + 1]
        return SimpleNamespace(returncode=9, stdout="brief out",
                               stderr="brief err")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(CodexCliError) as caught:
        run_codex_json("do not leak me", _schema(tmp_path),
                       codex_bin="codex-test-bin")
    assert "exited 9" in str(caught.value)
    assert "brief err" in str(caught.value)
    assert "do not leak me" not in str(caught.value)
    assert not Path(seen["output_path"]).exists()


def test_timeout_raises_and_cleans_temp_output(monkeypatch, tmp_path):
    seen = {}

    def fake_run(command, **kwargs):
        seen["output_path"] = command[
            command.index("--output-last-message") + 1]
        raise subprocess.TimeoutExpired(command, kwargs["timeout"],
                                        output="partial", stderr="late")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(CodexCliError, match="timed out"):
        run_codex_json("prompt", _schema(tmp_path), timeout=1,
                       codex_bin="codex-test-bin")
    assert not Path(seen["output_path"]).exists()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (None, "missing"),
        ("", "empty"),
        ("not-json", "not valid JSON"),
        ("[]", "not a JSON object"),
    ],
)
def test_rejects_unusable_output(monkeypatch, tmp_path, payload, expected):
    seen = {}

    def fake_run(command, **kwargs):
        output_path = command[command.index("--output-last-message") + 1]
        seen["output_path"] = output_path
        if payload is not None:
            with open(output_path, "w", encoding="utf-8") as stream:
                stream.write(payload)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(CodexCliError, match=expected):
        run_codex_json("prompt", _schema(tmp_path),
                       codex_bin="codex-test-bin")
    assert not Path(seen["output_path"]).exists()
