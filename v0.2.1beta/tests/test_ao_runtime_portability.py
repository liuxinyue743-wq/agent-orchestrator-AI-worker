"""Portable AO executable, runfile, endpoint, and CLI environment contract."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

import run_mission
from loopcore.action_executor import ActionExecutor
from loopcore.ao_adapter import AOAdapter, AOError
from loopcore.closed_loop import ClosedLoop
from loopcore.state_store import StateStore


def test_resolve_ao_bin_prefers_clao_override(tmp_path):
    executable = tmp_path / "ao.exe"
    executable.write_bytes(b"fake")

    def unexpected_which(_name):
        raise AssertionError("PATH lookup must not run for CLAO_AO_BIN")

    resolved = run_mission.resolve_ao_bin(
        environ={"CLAO_AO_BIN": str(executable)}, which=unexpected_which)
    assert resolved == str(executable)


def test_resolve_ao_bin_uses_path_when_unconfigured(tmp_path):
    executable = tmp_path / "ao.exe"
    executable.write_bytes(b"fake")
    calls = []

    def fake_which(name):
        calls.append(name)
        return str(executable)

    assert run_mission.resolve_ao_bin(environ={}, which=fake_which) == str(
        executable)
    assert calls == ["ao"]


def test_resolve_ao_bin_fails_fast_with_operator_hint():
    with pytest.raises(RuntimeError, match="CLAO_AO_BIN"):
        run_mission.resolve_ao_bin(environ={}, which=lambda _name: None)


def test_resolve_ao_run_file_default_and_override(tmp_path):
    assert run_mission.resolve_ao_run_file(
        environ={}, home=tmp_path) == tmp_path / ".ao" / "running.json"

    override = tmp_path / "custom" / "ao.json"
    assert run_mission.resolve_ao_run_file(
        environ={"CLAO_AO_RUN_FILE": str(override)},
        home=tmp_path) == override


def test_adapter_prefers_valid_runfile_port_then_config(tmp_path):
    run_file = tmp_path / "running.json"
    run_file.write_text('{"pid": 42, "port": 4567}', encoding="utf-8")

    dynamic = AOAdapter(
        base_url="http://127.0.0.1:4111", timeout=7, run_file=run_file)
    assert dynamic.base_url == "http://127.0.0.1:4567"
    assert dynamic.timeout == 7

    fallback = AOAdapter(
        base_url="http://127.0.0.1:4111", run_file=tmp_path / "missing")
    assert fallback.base_url == "http://127.0.0.1:4111"


def test_adapter_honors_ao_run_file_and_home_default(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.json"
    explicit.write_text('{"port": 4568}', encoding="utf-8")
    monkeypatch.setenv("AO_RUN_FILE", str(explicit))
    assert AOAdapter().base_url == "http://127.0.0.1:4568"

    monkeypatch.delenv("AO_RUN_FILE")
    default = tmp_path / ".ao" / "running.json"
    default.parent.mkdir()
    default.write_text('{"port": 4569}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert AOAdapter().base_url == "http://127.0.0.1:4569"


def test_adapter_get_session_workspace_uses_desktop_endpoint(
        tmp_path, monkeypatch):
    adapter = AOAdapter(run_file=tmp_path / "missing")
    calls = []

    def fake_get(path):
        calls.append(path)
        return {"sessionId": "project-7", "workspacePath": "C:/ao/wt"}

    monkeypatch.setattr(adapter, "_get", fake_get)
    assert adapter.get_session_workspace("project-7") == "C:/ao/wt"
    assert calls == ["/api/v1/desktop/sessions/project-7/workspace"]


@pytest.mark.parametrize("payload", [
    {}, {"workspacePath": None}, {"workspacePath": ""},
    {"workspacePath": "   "},
])
def test_adapter_get_session_workspace_rejects_empty_path(
        tmp_path, monkeypatch, payload):
    adapter = AOAdapter(run_file=tmp_path / "missing")
    monkeypatch.setattr(adapter, "_get", lambda _path: payload)
    with pytest.raises(AOError, match="workspacePath"):
        adapter.get_session_workspace("project-8")


def test_adapter_get_session_workspace_preserves_http_error(
        tmp_path, monkeypatch):
    adapter = AOAdapter(run_file=tmp_path / "missing")

    def missing(_path):
        raise AOError("HTTP 404 SESSION_WORKSPACE_NOT_FOUND")

    monkeypatch.setattr(adapter, "_get", missing)
    with pytest.raises(AOError, match="SESSION_WORKSPACE_NOT_FOUND"):
        adapter.get_session_workspace("project-9")


def test_closed_loop_worktree_path_uses_adapter_not_data_dir(
        tmp_path, monkeypatch):
    class Adapter:
        def __init__(self):
            self.calls = []

        def get_session_workspace(self, session_id):
            self.calls.append(session_id)
            return "C:/authoritative/workspace"

    loop = object.__new__(ClosedLoop)
    loop.task = SimpleNamespace(worker_session_id="project-10")
    loop.adapter = Adapter()
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path / "poison"))

    assert loop._worktree_path() == "C:/authoritative/workspace"
    assert loop.adapter.calls == ["project-10"]


def test_closed_loop_worktree_path_fails_closed_on_ao_error():
    class Adapter:
        def get_session_workspace(self, _session_id):
            raise AOError("SESSION_WORKSPACE_NOT_FOUND")

    loop = object.__new__(ClosedLoop)
    loop.task = SimpleNamespace(worker_session_id="project-11")
    loop.adapter = Adapter()
    assert loop._worktree_path() is None

    loop.task.worker_session_id = None
    assert loop._worktree_path() is None


def test_action_executor_only_adds_explicit_runfile(monkeypatch, tmp_path):
    monkeypatch.delenv("AO_DATA_DIR", raising=False)
    monkeypatch.delenv("AO_RUN_FILE", raising=False)
    run_file = tmp_path / "running.json"

    executor = ActionExecutor(
        "ao", str(tmp_path / "nonexistent-data"), str(run_file), object())
    child_env = executor._env()
    assert "AO_DATA_DIR" not in child_env
    assert child_env["AO_RUN_FILE"] == str(run_file)

    without_runfile = ActionExecutor("ao", None, None, object())._env()
    assert "AO_DATA_DIR" not in without_runfile
    assert "AO_RUN_FILE" not in without_runfile


def test_build_runtime_resolves_one_shared_ao_contract(monkeypatch, tmp_path):
    calls = {"bin": 0, "run_file": 0}
    captured = {}
    run_file = tmp_path / "running.json"

    def resolve_bin():
        calls["bin"] += 1
        return "fake-ao"

    def resolve_run_file():
        calls["run_file"] += 1
        return run_file

    class DummyRuntime:
        def __init__(self, mission, cfg, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(run_mission, "resolve_ao_bin", resolve_bin)
    monkeypatch.setattr(run_mission, "resolve_ao_run_file", resolve_run_file)
    monkeypatch.setattr(
        run_mission, "mission_preflight",
        lambda *_args, **_kwargs: {
            "ao_bin": resolve_bin(), "ao_run_file": resolve_run_file(),
            "project_path": tmp_path})
    monkeypatch.setattr(run_mission, "MissionRuntime", DummyRuntime)
    monkeypatch.delenv("AO_DATA_DIR", raising=False)

    run_mission.build_runtime({"mission_id": "M-PORTABLE"}, {})

    assert calls == {"bin": 1, "run_file": 1}
    assert captured["ao_bin"] == "fake-ao"
    assert captured["ao_run_file"] == run_file
    assert run_mission.os.environ["AO_RUN_FILE"] == str(run_file)
    assert "AO_DATA_DIR" not in run_mission.os.environ


def test_read_only_attach_skips_ao_but_normal_start_fails_fast(
        monkeypatch, tmp_path):
    from panel import server

    mission_id = "M-ATTACH-PORTABLE"
    mission = {
        "mission_id": mission_id,
        "project_id": "project",
        "objective": "inspect stored state",
        "allowed_paths": ["src/**"],
        "forbidden_paths": [".git/**"],
        "acceptance_criteria": [
            {"id": "AC1", "description": "stored state is readable"}],
        "gate_commands": [],
    }
    runtime_dir = tmp_path / "runtime" / mission_id
    runtime_dir.mkdir(parents=True)
    store = StateStore(str(runtime_dir / "state.db"))
    store.record_mission(
        mission_id, {"state": "MISSION_DONE", "mission": mission})
    store.close()

    cfg = run_mission.load_config()
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(run_mission, "ROOT", tmp_path)
    monkeypatch.setattr(run_mission, "load_config", lambda: cfg)
    monkeypatch.setattr(
        run_mission.shutil, "which",
        lambda name: "git.exe" if name == "git" else None)
    monkeypatch.delenv("CLAO_AO_BIN", raising=False)
    monkeypatch.delenv("AO_RUN_FILE", raising=False)
    panel = server.PanelState()
    monkeypatch.setattr(server, "PANEL", panel)

    def forbidden_external_call(*_args, **_kwargs):
        raise AssertionError("read-only attach attempted an external AO call")

    monkeypatch.setattr(run_mission.AOAdapter, "_get", forbidden_external_call)
    monkeypatch.setattr(
        run_mission.ActionExecutor, "_run", forbidden_external_call)
    monkeypatch.setattr(
        "loopcore.planner_adapter.run_codex_json", forbidden_external_call)
    monkeypatch.setattr(
        "loopcore.auditor.run_codex_json", forbidden_external_call)
    monkeypatch.setattr(
        "loopcore.verifier.run_codex_json", forbidden_external_call)

    attached = server.Handler._attach(
        object(), {"mission_id": mission_id})
    assert attached == {
        "ok": True, "mission_id": mission_id, "attached": True}
    assert panel.rt.mission.mission_id == mission_id
    assert panel.rt.executor.ao_bin == "ao-unavailable-read-only"
    assert panel.rt.store._conn.execute(
        "SELECT COUNT(*) FROM missions").fetchone()[0] == 1
    panel.rt.close()

    normal_panel = server.PanelState()
    monkeypatch.setattr(server, "PANEL", normal_panel)
    # This test is about missing AO, not the host's Python version. Keep the
    # Python requirement satisfied in this otherwise fully mocked preflight.
    monkeypatch.setattr(run_mission.sys, "version_info", (3, 12, 7))
    monkeypatch.setattr(run_mission.platform, "python_implementation",
                        lambda: "CPython")
    with pytest.raises(RuntimeError, match="CLAO_AO_BIN"):
        normal_panel.start_mission(mission)


def test_portability_code_does_not_scan_registry_or_install_ao():
    tree = ast.parse(Path(run_mission.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "winreg" not in imported
    assert "requests" not in imported
