from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    os.name != "nt" or shutil.which("powershell.exe") is None,
    reason="requires Windows PowerShell 5.1",
)


_FAKE_CLAO_SOURCE = r"""
using System;
using System.IO;
using System.Text;

public static class FakeClao
{
    public static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        string argvPath = Environment.GetEnvironmentVariable("CLAO_DEMO_TEST_ARGV");
        if (!String.IsNullOrEmpty(argvPath))
        {
            File.WriteAllLines(argvPath, args, new UTF8Encoding(false));
        }
        string cwdPath = Environment.GetEnvironmentVariable("CLAO_DEMO_TEST_CWD");
        if (!String.IsNullOrEmpty(cwdPath))
        {
            File.WriteAllText(cwdPath, Environment.CurrentDirectory);
        }

        if (Environment.GetEnvironmentVariable("CLAO_DEMO_TEST_MODE") == "error")
        {
            Console.WriteLine(
                "{\"error\":{\"code\":\"usage_error\",\"message\":" +
                "\"argument --gate-command-json: must be valid JSON\"}}"
            );
            return 1;
        }

        if (Environment.GetEnvironmentVariable("CLAO_DEMO_TEST_MODE") == "pass")
        {
            Console.WriteLine(
                "{\"gate\":{\"passed\":true},\"auditedResult\":null}"
            );
            return 0;
        }

        Console.WriteLine(
            "{\"gate\":{\"passed\":false,\"steps\":[{\"exit_code\":7," +
            "\"stdout\":\"DEMO-GATE-FAIL\"}]},\"gateAuditId\":" +
            "\"demo-run:gate\",\"auditedResult\":{\"decision\":\"LOCAL_FIX\"," +
            "\"workerResponse\":\"DEMO-GATE-ACK demo-run:gate\"}}"
        );
        return 3;
    }
}
"""


def _run_powershell(
    *arguments: str,
    env: dict[str, str],
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=cwd,
    )


def _compile_fake_clao(output_path: Path) -> None:
    env = os.environ.copy()
    env["CLAO_DEMO_TEST_SOURCE"] = _FAKE_CLAO_SOURCE
    env["CLAO_DEMO_TEST_EXE"] = str(output_path)
    completed = _run_powershell(
        "-Command",
        (
            "$ErrorActionPreference = 'Stop'; "
            "Add-Type -TypeDefinition $env:CLAO_DEMO_TEST_SOURCE "
            "-OutputAssembly $env:CLAO_DEMO_TEST_EXE "
            "-OutputType ConsoleApplication"
        ),
        env=env,
    )
    assert completed.returncode == 0, completed.stderr


def test_demo_uses_absolute_gate_python_and_preserves_json_in_powershell_51(
    tmp_path: Path,
) -> None:
    env = os.environ.copy()
    version = _run_powershell(
        "-Command",
        "$PSVersionTable.PSVersion.ToString(2)",
        env=env,
    )
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip() == "5.1"

    repo = tmp_path / "demo repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(Path(__file__).parents[1] / "scripts" / "demo.ps1", scripts)
    clao_path = repo / ".venv" / "Scripts" / "clao.exe"
    clao_path.parent.mkdir(parents=True)
    _compile_fake_clao(clao_path)

    gate_repo = tmp_path / "Gate Repo"
    gate_python = gate_repo / ".venv" / "Scripts" / "python.exe"
    gate_python.parent.mkdir(parents=True)
    gate_python.touch()
    launch_cwd = tmp_path / "Launch Cwd"
    launch_cwd.mkdir()
    assert launch_cwd.resolve() != gate_repo.resolve()

    argv_path = tmp_path / "argv.txt"
    cwd_path = tmp_path / "cwd.txt"
    env["CLAO_DEMO_TEST_ARGV"] = str(argv_path)
    env["CLAO_DEMO_TEST_CWD"] = str(cwd_path)

    command = [
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(scripts / "demo.ps1"),
        "-AuditorSession",
        "auditor-1",
        "-PlannerSession",
        "planner-1",
        "-WorkerSession",
        "worker-1",
        "-GateRepo",
        str(gate_repo),
        "-RunId",
        "demo-run",
        "-Scenario",
        "Fail",
    ]
    completed = _run_powershell(*command, env=env, cwd=launch_cwd)

    assert completed.returncode == 0, completed.stderr
    assert "[demo] Fail: OK" in completed.stdout
    assert completed.stderr == ""
    assert Path(cwd_path.read_text(encoding="utf-8")).resolve() == launch_cwd.resolve()
    assert Path(cwd_path.read_text(encoding="utf-8")).resolve() != gate_repo.resolve()
    received = argv_path.read_text(encoding="utf-8").splitlines()
    command_indexes = [
        index for index, value in enumerate(received) if value == "--gate-command-json"
    ]
    assert command_indexes == [received.index("--gate-command-json")]
    command_json = received[command_indexes[0] + 1]
    expected_gate_python = str(gate_python.resolve())
    assert json.loads(command_json) == [
        expected_gate_python,
        "-c",
        "import sys; print('DEMO-GATE-FAIL'); sys.exit(7)",
    ]
    assert Path(json.loads(command_json)[0]).is_absolute()
    acceptance_criteria = [
        received[index + 1]
        for index, value in enumerate(received)
        if value == "--acceptance-criterion"
    ]
    assert (
        "The Auditor recommends LOCAL_FIX only when a Gate step has stdout "
        "exactly DEMO-GATE-FAIL and exit_code=7."
    ) in acceptance_criteria
    assert (
        "For any other unexpected Gate failure, the Auditor recommends HUMAN "
        "and no Worker feedback is sent."
    ) in acceptance_criteria

    env["CLAO_DEMO_TEST_MODE"] = "pass"
    pass_command = command[:-1] + ["Pass"]
    passed = _run_powershell(*pass_command, env=env, cwd=launch_cwd)

    assert passed.returncode == 0, passed.stderr
    assert "[demo] Pass: OK (exit=0, gate.passed=true, auditedResult=null)" in (
        passed.stdout
    )
    assert passed.stderr == ""
    received = argv_path.read_text(encoding="utf-8").splitlines()
    pass_command_json = [
        json.loads(received[index + 1])
        for index, value in enumerate(received)
        if value == "--gate-command-json"
    ]
    assert pass_command_json == [
        [expected_gate_python, "-m", "pytest"],
        [expected_gate_python, "-m", "compileall", "-q", "src"],
        [expected_gate_python, "-m", "pip", "check"],
    ]
    assert all(Path(command[0]).is_absolute() for command in pass_command_json)

    env["CLAO_DEMO_TEST_MODE"] = "error"
    failed = _run_powershell(*command, env=env, cwd=launch_cwd)

    assert failed.returncode == 1
    assert "[demo] ERROR: clao returned error JSON (exit 1):" in failed.stderr
    assert '"code":"usage_error"' in failed.stderr
    assert "argument --gate-command-json: must be valid JSON" in failed.stderr
