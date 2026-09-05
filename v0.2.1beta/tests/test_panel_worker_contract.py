"""Keep the Panel's production Mission payload on the Codex Worker contract."""

from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from panel import server as panel_server


SERVER = Path(__file__).resolve().parents[1] / "panel" / "server.py"
INDEX = SERVER.with_name("index.html")
PRODUCT_ROOT = SERVER.parents[1]
PRODUCT_ARCHITECTURE = PRODUCT_ROOT / "docs" / "ARCHITECTURE.md"


def _start_panel_mission(monkeypatch, tmp_path, max_subtasks=...):
    start = MagicMock()
    project_path = tmp_path / "registered-project"
    project_path.mkdir(exist_ok=True)
    monkeypatch.setattr(panel_server, "ROOT", tmp_path)
    monkeypatch.setattr(panel_server.PANEL, "start_mission", start)
    monkeypatch.setattr(panel_server, "_load_ao_projects", lambda: [{
        "id": "project-a", "name": "Project A", "path": str(project_path),
        "kind": "git",
    }])
    body = {
        "project_id": "project-a",
        "objective": "implement one function",
        "allowed_paths": "app.py",
        "acceptance_criteria": "the function works",
        "gate_commands": "python -m pytest -q",
    }
    if max_subtasks is not ...:
        body["max_subtasks"] = max_subtasks
    result = panel_server.Handler._start_mission(object(), body)
    assert result["ok"] is True
    return start.call_args.args[0]


def _valid_mission_body(project_id="project-a"):
    return {
        "project_id": project_id,
        "objective": "implement one function",
        "allowed_paths": "app.py",
        "acceptance_criteria": "the function works",
        "gate_commands": "python -m pytest -q",
    }


def test_project_api_uses_current_ao_registry_and_public_runtime_config(
        monkeypatch, tmp_path):
    run_file = tmp_path / "running.json"
    adapter = MagicMock()
    adapter.get_projects.return_value = [{
        "id": "project-a", "name": "Project A", "path": "C:/repo/a",
        "kind": "git", "internal": "must-not-leak",
    }]
    adapter_type = MagicMock(return_value=adapter)
    monkeypatch.setattr(panel_server, "AOAdapter", adapter_type)
    monkeypatch.setattr(panel_server.run_mission, "load_config", lambda: {
        "ao": {"base_url": "http://127.0.0.1:4321",
               "request_timeout_seconds": 9},
    })
    monkeypatch.setattr(panel_server.run_mission, "resolve_ao_run_file",
                        lambda: run_file)
    response = MagicMock()

    panel_server.Handler.do_GET(SimpleNamespace(
        path="/api/projects", _json=response))

    adapter_type.assert_called_once_with(
        base_url="http://127.0.0.1:4321", timeout=9.0,
        run_file=run_file)
    adapter.get_projects.assert_called_once_with()
    response.assert_called_once_with({
        "ok": True,
        "projects": [{"id": "project-a", "name": "Project A",
                      "path": "C:/repo/a", "kind": "git"}],
    })


def test_project_api_reports_ao_failure_without_fabricated_project(monkeypatch):
    monkeypatch.setattr(
        panel_server, "_load_ao_projects",
        MagicMock(side_effect=RuntimeError("AO daemon unavailable")))
    response = MagicMock()

    panel_server.Handler.do_GET(SimpleNamespace(
        path="/api/projects", _json=response))

    response.assert_called_once_with(
        {"ok": False, "error": "AO daemon unavailable"}, 503)
    assert "closed-loop-demo" not in str(response.call_args)


def test_panel_mission_payload_uses_codex_worker_harness():
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    start_mission = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_start_mission"
    )
    mission_dict = next(
        node.value for node in start_mission.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "mission"
                for target in node.targets)
        and isinstance(node.value, ast.Dict)
    )
    harness_values = [
        value.value
        for key, value in zip(mission_dict.keys, mission_dict.values)
        if isinstance(key, ast.Constant) and key.value == "worker_harness"
        and isinstance(value, ast.Constant)
    ]

    assert harness_values == ["codex"]
    assert not any(
        isinstance(node, ast.Constant) and node.value == "claude-code"
        for node in ast.walk(start_mission)
    )


def test_panel_and_cli_share_build_runtime():
    panel_tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    runner = SERVER.parents[1] / "run_mission.py"
    runner_tree = ast.parse(runner.read_text(encoding="utf-8"))

    def calls_build_runtime(tree, owner=None):
        scope = tree
        if owner:
            scope = next(
                node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == owner)
        return any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "run_mission"
            and node.func.attr == "build_runtime"
            for node in ast.walk(scope)
        ) if owner else any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_runtime"
            for node in ast.walk(scope)
        )

    assert calls_build_runtime(panel_tree, "start_mission")
    assert calls_build_runtime(runner_tree)


def test_retired_legacy_cli_entrypoints_stay_out_of_product_roots():
    loopcore = PRODUCT_ROOT / "src" / "loopcore"
    architecture = PRODUCT_ARCHITECTURE.read_text(encoding="utf-8")

    assert not (loopcore / "cli.py").exists()
    assert not (loopcore / "mission_cli.py").exists()
    assert not (loopcore / "closed_loop_cli.py").exists()
    assert (PRODUCT_ROOT / "run_mission.py").is_file()
    assert (PRODUCT_ROOT / "启动CLAO.bat").is_file()
    assert not (PRODUCT_ROOT / "启动面板.bat").exists()
    assert SERVER.is_file()
    assert "正式运行入口只有" in architecture
    assert "Panel" in architecture
    assert "`run_mission.py`" in architecture
    for retired_cli in ("loopcore.cli", "mission_cli.py", "closed_loop_cli.py"):
        assert retired_cli not in architecture


def test_panel_omitted_max_subtasks_defaults_to_one(monkeypatch, tmp_path):
    mission = _start_panel_mission(monkeypatch, tmp_path)
    assert mission["budgets"]["max_subtasks"] == 1
    assert mission["project_id"] == "project-a"


def test_panel_requires_project_id_before_project_discovery(
        monkeypatch, tmp_path):
    start = MagicMock()
    discovery = MagicMock(side_effect=AssertionError(
        "missing project_id must fail before AO discovery"))
    monkeypatch.setattr(panel_server, "ROOT", tmp_path)
    monkeypatch.setattr(panel_server.PANEL, "start_mission", start)
    monkeypatch.setattr(panel_server, "_load_ao_projects", discovery)
    body = _valid_mission_body()
    body.pop("project_id")

    with pytest.raises(RuntimeError, match="project_id is required"):
        panel_server.Handler._start_mission(object(), body)

    discovery.assert_not_called()
    start.assert_not_called()


def test_panel_normal_start_uses_shared_runtime_preflight(
        monkeypatch, tmp_path):
    panel = panel_server.PanelState()
    monkeypatch.setattr(panel_server.run_mission, "setup_environment",
                        lambda **_kwargs: None)
    monkeypatch.setattr(panel_server.run_mission, "load_config", lambda: {})
    build = MagicMock(side_effect=panel_server.run_mission.PreflightError(
        "AO Project is not registered: unknown"))
    monkeypatch.setattr(panel_server.run_mission, "build_runtime", build)

    with pytest.raises(panel_server.run_mission.PreflightError,
                       match="not registered"):
        panel.start_mission({"mission_id": "M-PANEL", "project_id": "unknown"})

    build.assert_called_once()
    assert panel.rt is None
    assert panel.thread is None


@pytest.mark.parametrize("value", [1, 2])
def test_panel_accepts_one_or_two_workers(monkeypatch, tmp_path, value):
    mission = _start_panel_mission(monkeypatch, tmp_path, value)
    assert mission["budgets"]["max_subtasks"] == value


@pytest.mark.parametrize("value", [0, -1, 3])
def test_panel_rejects_out_of_range_workers(monkeypatch, tmp_path, value):
    start = MagicMock()
    project_path = tmp_path / "registered-project"
    project_path.mkdir()
    monkeypatch.setattr(panel_server, "ROOT", tmp_path)
    monkeypatch.setattr(panel_server.PANEL, "start_mission", start)
    monkeypatch.setattr(panel_server, "_load_ao_projects", lambda: [{
        "id": "project-a", "name": "Project A", "path": str(project_path),
        "kind": "git",
    }])
    body = {
        "project_id": "project-a",
        "objective": "implement one function",
        "allowed_paths": "app.py",
        "acceptance_criteria": "the function works",
        "gate_commands": "python -m pytest -q",
        "max_subtasks": value,
    }
    with pytest.raises(ValueError, match="must be 1 or 2"):
        panel_server.Handler._start_mission(object(), body)
    start.assert_not_called()


def test_panel_frontend_defaults_to_bounded_single_worker():
    html = INDEX.read_text(encoding="utf-8")
    assert ('id="f_sub" type="number" min="1" max="2" value="1"'
            in html)
    assert 'max_subtasks:Number($("f_sub").value)' in html
    assert 'max_subtasks:+$("f_sub").value||2' not in html


def test_panel_frontend_loads_and_selects_ao_projects():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="f_project" disabled' in html
    assert 'fetch("/api/projects")' in html
    assert 'option.textContent=`${p.name} (${p.id})`' in html
    assert '$("f_project").onchange=showSelectedProject' in html
    assert 'Project path：${selected.path || "—"}' in html
    assert 'kind：${selected.kind || "—"}' in html
    assert 'option.textContent="AO 中没有已注册项目"' in html
    assert 'error.textContent=e.message' in html
    assert 'selector.disabled=false; start.disabled=false' in html
    assert 'project_id:$("f_project").value' in html
    assert "project_path:" not in html
    assert "project_name:" not in html


def test_panel_frontend_does_not_expose_auto_master_writeback():
    html = INDEX.read_text(encoding="utf-8")
    assert "k_ff" not in html
    assert "auto_ff_master" not in html
    assert "DONE 后自动合并 master" not in html
    assert "auto_ff" not in html


def test_historical_resume_keeps_stored_project_id(monkeypatch, tmp_path):
    mission_id = "M-HISTORICAL-PROJECT"
    mission = {
        "mission_id": mission_id,
        "project_id": "historical-project",
        "objective": "resume existing state",
    }
    db = tmp_path / "runtime" / mission_id / "state.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE missions (payload_json TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO missions(payload_json) VALUES (?)",
            (json.dumps({"mission": mission}),),
        )
        conn.commit()
    finally:
        conn.close()
    start = MagicMock()
    monkeypatch.setattr(panel_server, "ROOT", tmp_path)
    monkeypatch.setattr(panel_server.PANEL, "start_mission", start)
    monkeypatch.setattr(
        panel_server, "_load_ao_projects",
        MagicMock(side_effect=AssertionError(
            "resume must not use the new-Mission selector")))

    result = panel_server.Handler._resume(
        object(), {"mission_id": mission_id})

    assert result == {"ok": True, "mission_id": mission_id, "resumed": True}
    assert start.call_args.args[0]["project_id"] == "historical-project"


def test_panel_project_selector_has_no_demo_or_ao_database_fallback():
    source = SERVER.read_text(encoding="utf-8")

    assert 'body.get("project_id") or "closed-loop-demo"' not in source
    assert "AO_DATA_DIR" not in source
    assert "ao.db" not in source


def test_panel_snapshot_config_has_only_live_time_parameters(
        monkeypatch, tmp_path):
    panel = panel_server.PanelState()
    monkeypatch.setattr(panel_server, "PANEL", panel)
    monkeypatch.setattr(panel_server, "ROOT", tmp_path)

    config = panel_server.snapshot()["config"]

    assert config == panel.live
    assert "auto_ff_master" not in config


def test_panel_config_rejects_auto_master_writeback_true():
    panel = panel_server.PanelState()

    with pytest.raises(
            RuntimeError,
            match="auto_ff_master is disabled in the competition runtime"):
        panel.set_config({"auto_ff_master": True})


def test_panel_config_ignores_legacy_false_and_applies_time_parameters():
    panel = panel_server.PanelState()

    config = panel.set_config({
        "auto_ff_master": False,
        "poll_seconds": 9,
        "idle_audit_cooldown_seconds": 17,
    })

    assert config["poll_seconds"] == 9
    assert config["idle_audit_cooldown_seconds"] == 17
    assert "auto_ff_master" not in config


def test_panel_mission_done_has_no_scm_writeback(monkeypatch):
    panel = panel_server.PanelState()
    controller = SimpleNamespace(state="MISSION_DONE", step=MagicMock())
    projector = SimpleNamespace(project_once=MagicMock())
    panel.rt = SimpleNamespace(
        controller=controller,
        projector=projector,
        mission=SimpleNamespace(
            mission_id="M-COMPETITION-DONE", project_id="project"),
    )

    def forbidden_subprocess(*_args, **_kwargs):
        raise AssertionError("MISSION_DONE attempted an SCM subprocess")

    monkeypatch.setattr("subprocess.run", forbidden_subprocess)
    panel._run()

    controller.step.assert_called_once_with()
    assert projector.project_once.call_count == 2
    assert panel.last_summary == {
        "mission_id": "M-COMPETITION-DONE",
        "final_state": "MISSION_DONE",
        "stopped_by_user": False,
    }
    assert panel.errors == []


def test_panel_runtime_contains_no_auto_ff_or_git_subprocess():
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))

    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "ff_master_to_integration"
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Import)
        and any(alias.name == "subprocess" for alias in node.names)
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module == "subprocess"
        for node in ast.walk(tree)
    )
    assert not any(
        {"git", operation}.issubset({
            child.value
            for child in ast.walk(node)
            if isinstance(child, ast.Constant)
            and isinstance(child.value, str)
        })
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for operation in ("push", "merge")
    )
