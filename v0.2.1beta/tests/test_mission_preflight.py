"""Offline checks for the shared CLI/Panel Mission preflight boundary."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import run_mission
from loopcore.ao_adapter import AOError


@pytest.fixture(autouse=True)
def _supported_python_capability(monkeypatch):
    """Mock the supported Python capability, as Git/AO/Codex are mocked.

    Version rejection cases override these values explicitly. This fixture
    tests decision branches on any host, not an actual Python installation.
    It is restored by pytest after each test. Production still requires 3.12.
    """
    monkeypatch.setattr(run_mission.sys, "version_info", (3, 12, 7))
    monkeypatch.setattr(run_mission.platform, "python_implementation",
                        lambda: "CPython")


MISSION = {
    "mission_id": "M-PREFLIGHT",
    "project_id": "project-a",
    "objective": "verify preflight",
    "allowed_paths": ["app.py"],
    "forbidden_paths": [".git/**"],
    "acceptance_criteria": [{"id": "AC1", "description": "passes"}],
    "gate_commands": ["python -m pytest -q"],
    "budgets": {"max_subtasks": 1},
}

CFG = {
    "ao": {"base_url": "http://127.0.0.1:3001",
           "request_timeout_seconds": 3},
    "roles": {
        "planner": {"model": "planner-model"},
        "auditor": {"model": "auditor-model"},
        "verifier": {"model": "verifier-model"},
    },
    "worker": {"model": "worker-model"},
}


def _completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(
        returncode=returncode, stdout=stdout, stderr=stderr)


def _happy_environment(monkeypatch, tmp_path, default_branch="main"):
    project = tmp_path / "project"
    project.mkdir()
    run_file = tmp_path / "running.json"
    run_file.write_text('{"port": 3001}', encoding="utf-8")
    executables = {
        "git": str(tmp_path / "git.exe"),
        "ao": str(tmp_path / "ao.exe"),
        "codex": str(tmp_path / "codex.exe"),
    }
    monkeypatch.setattr(
        run_mission.shutil, "which", lambda name: executables.get(name))
    monkeypatch.setattr(
        run_mission, "resolve_ao_run_file", lambda: run_file)

    class Adapter:
        def __init__(self, **_kwargs):
            pass

        def get_projects(self):
            return [{"id": "project-a", "path": str(project),
                     "name": "Project A", "kind": "single_repo"}]

        def get_project(self, project_id):
            assert project_id == "project-a"
            return {"id": project_id, "path": str(project),
                    "kind": "single_repo",
                    "defaultBranch": default_branch}

    monkeypatch.setattr(run_mission, "AOAdapter", Adapter)

    def command(argv, **_kwargs):
        if argv[1:] == ["remote"]:
            return _completed(stdout="origin\n")
        if "rev-parse" in argv:
            return _completed(stdout="true\n")
        if "config" in argv:
            return _completed(stdout="configured\n")
        if argv[-2:] == ["login", "status"]:
            return _completed(stdout="Logged in using ChatGPT\n")
        raise AssertionError("unexpected preflight command: %r" % argv)

    monkeypatch.setattr(run_mission.subprocess, "run", command)
    return project, run_file, executables


def _branch_environment(monkeypatch, tmp_path, default_branch, responses):
    project, run_file, executables = _happy_environment(
        monkeypatch, tmp_path, default_branch=default_branch)
    calls = []

    def command(argv, **_kwargs):
        command_args = tuple(argv[1:])
        calls.append(command_args)
        if command_args == ("rev-parse", "--is-inside-work-tree"):
            return _completed(stdout="true\n")
        if command_args in (
                ("config", "--get", "user.name"),
                ("config", "--get", "user.email")):
            return _completed(stdout="configured\n")
        if command_args == ("login", "status"):
            return _completed(stdout="Logged in using ChatGPT\n")
        if command_args in responses:
            return responses[command_args]
        raise AssertionError("unexpected preflight command: %r" % (argv,))

    monkeypatch.setattr(run_mission.subprocess, "run", command)
    return project, run_file, executables, calls


@pytest.mark.parametrize("implementation,version", [
    ("PyPy", (3, 12, 7)),
    ("CPython", (3, 11, 9)),
    ("CPython", (3, 13, 5)),
    ("CPython", (3, 14, 0)),
])
def test_preflight_rejects_unsupported_python(
        monkeypatch, implementation, version):
    monkeypatch.setattr(
        run_mission.platform, "python_implementation",
        lambda: implementation)
    monkeypatch.setattr(run_mission.sys, "version_info", version)

    with pytest.raises(run_mission.PreflightError, match="CPython 3.12"):
        run_mission.mission_preflight(MISSION, CFG)


def test_preflight_rejects_missing_git(monkeypatch):
    monkeypatch.setattr(run_mission.shutil, "which", lambda _name: None)
    with pytest.raises(run_mission.PreflightError, match="Git executable"):
        run_mission.mission_preflight(MISSION, CFG)


def test_preflight_rejects_missing_ao_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(
        run_mission.shutil, "which",
        lambda name: "git.exe" if name == "git" else None)
    monkeypatch.delenv("CLAO_AO_BIN", raising=False)
    with pytest.raises(run_mission.PreflightError, match="AO executable"):
        run_mission.mission_preflight(MISSION, CFG)


def test_preflight_rejects_missing_ao_runfile(monkeypatch, tmp_path):
    _happy_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(
        run_mission, "resolve_ao_run_file", lambda: tmp_path / "missing")
    with pytest.raises(run_mission.PreflightError, match="runfile"):
        run_mission.mission_preflight(MISSION, CFG)


def test_preflight_rejects_unavailable_ao_api(monkeypatch, tmp_path):
    _happy_environment(monkeypatch, tmp_path)

    class UnavailableAdapter:
        def __init__(self, **_kwargs):
            pass

        def get_projects(self):
            raise AOError("connection refused")

    monkeypatch.setattr(run_mission, "AOAdapter", UnavailableAdapter)
    with pytest.raises(run_mission.PreflightError, match="daemon/API"):
        run_mission.mission_preflight(MISSION, CFG)


def test_preflight_rejects_unknown_project(monkeypatch, tmp_path):
    _happy_environment(monkeypatch, tmp_path)

    class EmptyAdapter:
        def __init__(self, **_kwargs):
            pass

        def get_projects(self):
            return []

    monkeypatch.setattr(run_mission, "AOAdapter", EmptyAdapter)
    with pytest.raises(run_mission.PreflightError, match="not registered"):
        run_mission.mission_preflight(MISSION, CFG)


def test_preflight_rejects_placeholder_project_before_ao(monkeypatch):
    monkeypatch.setattr(run_mission.shutil, "which", lambda _name: "git.exe")
    mission = dict(MISSION, project_id=run_mission.SAMPLE_PROJECT_PLACEHOLDER)
    with pytest.raises(run_mission.PreflightError, match="replace sample"):
        run_mission.mission_preflight(mission, CFG)


def test_preflight_rejects_missing_project_path(monkeypatch, tmp_path):
    _happy_environment(monkeypatch, tmp_path)

    class MissingPathAdapter:
        def __init__(self, **_kwargs):
            pass

        def get_projects(self):
            return [{"id": "project-a", "path": str(tmp_path / "missing")}]

    monkeypatch.setattr(run_mission, "AOAdapter", MissingPathAdapter)
    with pytest.raises(run_mission.PreflightError, match="path is unavailable"):
        run_mission.mission_preflight(MISSION, CFG)


def test_preflight_rejects_non_git_project(monkeypatch, tmp_path):
    _happy_environment(monkeypatch, tmp_path)

    def command(argv, **_kwargs):
        if "rev-parse" in argv:
            return _completed(returncode=128, stderr="not a repository")
        raise AssertionError("unexpected command")

    monkeypatch.setattr(run_mission.subprocess, "run", command)
    with pytest.raises(run_mission.PreflightError, match="Git worktree"):
        run_mission.mission_preflight(MISSION, CFG)


@pytest.mark.parametrize("missing_key", ["user.name", "user.email"])
def test_preflight_rejects_missing_git_identity(
        monkeypatch, tmp_path, missing_key):
    _happy_environment(monkeypatch, tmp_path)

    def command(argv, **_kwargs):
        if argv[1:] == ["remote"]:
            return _completed(stdout="origin\n")
        if "rev-parse" in argv:
            return _completed(stdout="true\n")
        if argv[-1] == missing_key:
            return _completed(returncode=1)
        if "config" in argv:
            return _completed(stdout="configured\n")
        raise AssertionError("Codex must not run after missing Git identity")

    monkeypatch.setattr(run_mission.subprocess, "run", command)
    with pytest.raises(run_mission.PreflightError, match=missing_key):
        run_mission.mission_preflight(MISSION, CFG)


def test_preflight_rejects_explicit_local_branch_without_origin(
        monkeypatch, tmp_path):
    responses = {
        ("remote",): _completed(stdout=""),
    }
    project, *_rest, calls = _branch_environment(
        monkeypatch, tmp_path, "main", responses)
    monkeypatch.setattr(run_mission, "ROOT", tmp_path)

    with pytest.raises(
            run_mission.PreflightError,
            match="has no origin remote required"):
        run_mission.mission_preflight(MISSION, CFG)

    assert not (tmp_path / "runtime").exists()
    assert not any("refs/heads/main" in value
                   for call in calls for value in call)
    assert not any(call and call[0] in ("fetch", "ls-remote")
                   for call in calls)
    assert project.is_dir()


def test_preflight_accepts_explicit_origin_branch(
        monkeypatch, tmp_path):
    responses = {
        ("remote",): _completed(stdout="origin\n"),
        ("rev-parse", "--verify", "--quiet",
         "refs/remotes/origin/main^{commit}"):
            _completed(stdout="abc123\n"),
    }
    _branch_environment(monkeypatch, tmp_path, "main", responses)

    result = run_mission.mission_preflight(MISSION, CFG)

    assert result["project_path"] == tmp_path / "project"


def test_preflight_rejects_missing_explicit_origin_branch(
        monkeypatch, tmp_path):
    responses = {
        ("remote",): _completed(stdout="origin\n"),
        ("rev-parse", "--verify", "--quiet",
         "refs/remotes/origin/main^{commit}"):
            _completed(returncode=1),
    }
    _branch_environment(monkeypatch, tmp_path, "main", responses)

    with pytest.raises(
            run_mission.PreflightError,
            match="remote-backed base refs/remotes/origin/main is unavailable"):
        run_mission.mission_preflight(MISSION, CFG)


def test_preflight_auto_without_remotes_fails_without_branch_fallback(
        monkeypatch, tmp_path):
    responses = {
        ("remote",): _completed(stdout=""),
    }
    project, *_rest, calls = _branch_environment(
        monkeypatch, tmp_path, "auto", responses)
    monkeypatch.setattr(run_mission, "ROOT", tmp_path)

    with pytest.raises(
            run_mission.PreflightError,
            match="has no origin remote required"):
        run_mission.mission_preflight(MISSION, CFG)

    assert not (tmp_path / "runtime").exists()
    assert not any("refs/heads/main" in value
                   for call in calls for value in call)
    assert project.is_dir()


def test_preflight_auto_origin_uses_cached_symbolic_head(
        monkeypatch, tmp_path):
    responses = {
        ("remote",): _completed(stdout="origin\n"),
        ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"):
            _completed(stdout="refs/remotes/origin/main\n"),
        ("rev-parse", "--verify", "--quiet",
         "refs/remotes/origin/main^{commit}"):
            _completed(stdout="abc123\n"),
    }
    _branch_environment(monkeypatch, tmp_path, "auto", responses)

    assert run_mission.mission_preflight(MISSION, CFG)["project_path"] == \
        tmp_path / "project"


def test_preflight_auto_rejects_missing_origin_head(
        monkeypatch, tmp_path):
    responses = {
        ("remote",): _completed(stdout="origin\n"),
        ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"):
            _completed(returncode=1),
    }
    _branch_environment(monkeypatch, tmp_path, "auto", responses)

    with pytest.raises(
            run_mission.PreflightError,
            match="refs/remotes/origin/HEAD is unavailable"):
        run_mission.mission_preflight(MISSION, CFG)


def test_preflight_does_not_fallback_to_checked_out_main(
        monkeypatch, tmp_path):
    responses = {
        ("remote",): _completed(stdout=""),
    }
    *_values, calls = _branch_environment(
        monkeypatch, tmp_path, "main", responses)

    with pytest.raises(run_mission.PreflightError):
        run_mission.mission_preflight(MISSION, CFG)

    assert ("symbolic-ref", "--quiet", "--short", "HEAD") not in calls
    assert not any("refs/heads/main" in value
                   for call in calls for value in call)


def test_adapter_project_detail_uses_official_endpoint(monkeypatch):
    adapter = run_mission.AOAdapter()
    seen = []
    monkeypatch.setattr(
        adapter, "_get",
        lambda path: seen.append(path) or {
            "project": {"id": "project/a", "defaultBranch": "main"}})

    assert adapter.get_project("project/a")["defaultBranch"] == "main"
    assert seen == ["/api/v1/projects/project%2Fa"]


def test_preflight_rejects_missing_codex(monkeypatch, tmp_path):
    _happy_environment(monkeypatch, tmp_path)
    original = run_mission.shutil.which
    monkeypatch.setattr(
        run_mission.shutil, "which",
        lambda name: None if name == "codex" else original(name))
    with pytest.raises(run_mission.PreflightError, match="Codex CLI"):
        run_mission.mission_preflight(MISSION, CFG)


def test_preflight_rejects_non_chatgpt_login(monkeypatch, tmp_path):
    _happy_environment(monkeypatch, tmp_path)

    def command(argv, **_kwargs):
        if argv[1:] == ["remote"]:
            return _completed(stdout="origin\n")
        if "rev-parse" in argv:
            return _completed(stdout="true\n")
        if "config" in argv:
            return _completed(stdout="configured\n")
        return _completed(stdout="Not logged in")

    monkeypatch.setattr(run_mission.subprocess, "run", command)
    with pytest.raises(run_mission.PreflightError, match="ChatGPT"):
        run_mission.mission_preflight(MISSION, CFG)


@pytest.mark.parametrize("config_path", [
    ("roles", "planner"),
    ("roles", "auditor"),
    ("roles", "verifier"),
    ("worker", None),
])
def test_preflight_rejects_missing_production_model(
        monkeypatch, tmp_path, config_path):
    _happy_environment(monkeypatch, tmp_path)
    cfg = json.loads(json.dumps(CFG))
    section, role = config_path
    if role is None:
        cfg[section]["model"] = "  "
    else:
        cfg[section][role]["model"] = ""

    with pytest.raises(run_mission.PreflightError, match="model configuration"):
        run_mission.mission_preflight(MISSION, cfg)


def test_preflight_happy_path(monkeypatch, tmp_path):
    project, run_file, executables = _happy_environment(monkeypatch, tmp_path)
    result = run_mission.mission_preflight(MISSION, CFG)
    assert result == {
        "ao_bin": executables["ao"],
        "ao_run_file": run_file,
        "project_path": project,
    }


def test_build_runtime_preflight_failure_creates_no_runtime(
        monkeypatch, tmp_path):
    monkeypatch.setattr(run_mission, "ROOT", tmp_path)
    monkeypatch.setattr(
        run_mission, "mission_preflight",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            run_mission.PreflightError("blocked")))

    with pytest.raises(run_mission.PreflightError, match="blocked"):
        run_mission.build_runtime(MISSION, CFG)

    assert not (tmp_path / "runtime").exists()


def test_deterministic_dry_run_skips_mission_preflight(
        monkeypatch, tmp_path, capsys):
    mission_path = tmp_path / "mission.json"
    mission_path.write_text(json.dumps(MISSION), encoding="utf-8")
    monkeypatch.setattr(run_mission, "load_config", lambda: CFG)
    monkeypatch.setattr(
        run_mission, "mission_preflight",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("deterministic dry-run entered Mission preflight")))
    monkeypatch.setattr(
        run_mission.sys, "argv", ["run_mission.py", str(mission_path),
                                  "--dry-run"])

    assert run_mission.main() == 0
    assert '"subtask_count": 1' in capsys.readouterr().out
    assert not (tmp_path / "runtime").exists()


def test_cli_reports_bounded_preflight_failure_without_traceback(
        monkeypatch, tmp_path, capsys):
    mission_path = tmp_path / "mission.json"
    mission_path.write_text(json.dumps(MISSION), encoding="utf-8")
    monkeypatch.setattr(run_mission, "load_config", lambda: CFG)
    monkeypatch.setattr(run_mission, "setup_environment", lambda **_kwargs: None)
    monkeypatch.setattr(
        run_mission, "build_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            run_mission.PreflightError("AO offline\nprivate detail")))
    monkeypatch.setattr(
        run_mission.sys, "argv", ["run_mission.py", str(mission_path)])

    assert run_mission.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "preflight failed: AO offline private detail\n"
    assert "Traceback" not in captured.err
