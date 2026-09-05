[CmdletBinding()]
param([switch]$SkipBootstrap)

$ErrorActionPreference = "Stop"
$ProductRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$Python = Join-Path $ProductRoot ".venv\Scripts\python.exe"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$LogRoot = Join-Path $ProductRoot (".local-checks\" + $Stamp)
$PreviousPath = $env:PATH
$PreviousPythonPath = $env:PYTHONPATH
$PreviousPlugins = $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD
$ExitCode = 0

function Invoke-PythonCheck {
    param([string]$Name, [string[]]$Arguments)
    Write-Host ("[CHECK] " + $Name)
    & $script:Python @Arguments 2>&1 |
        Tee-Object -FilePath (Join-Path $script:LogRoot ($Name + ".txt"))
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw ("Check '" + $Name + "' failed (exit " + $code + "). No AO/model was started.")
    }
}

Push-Location $ProductRoot
try {
    New-Item -ItemType Directory -Path $LogRoot | Out-Null
    if (-not $SkipBootstrap) {
        # A child PowerShell isolates bootstrap's `exit` from this script.
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProductRoot "bootstrap.ps1")
        if ($LASTEXITCODE -ne 0) { throw "bootstrap failed; preserve the error and stop" }
    }
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "Local Python was not found. Run bootstrap.ps1 first."
    }
    $env:PATH = (Split-Path -Parent $Python) + [IO.Path]::PathSeparator + $PreviousPath
    $env:PYTHONPATH = (Join-Path $ProductRoot "src")
    $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"

    Invoke-PythonCheck -Name "environment" -Arguments @("-c", "import platform,sys,jsonschema,yaml,pytest; from importlib.metadata import version; assert platform.python_implementation() == 'CPython' and sys.version_info[:2] == (3,12); print('Python=' + platform.python_version()); print('jsonschema=' + version('jsonschema')); print('pytest=' + pytest.__version__); print('PyYAML=' + yaml.__version__)")
    Invoke-PythonCheck -Name "dependency-check" -Arguments @("-m", "pip", "check")
    Invoke-PythonCheck -Name "pytest" -Arguments @("-m", "pytest", "tests", "-q", "--junitxml", (Join-Path $LogRoot "pytest.xml"))
    Invoke-PythonCheck -Name "compile" -Arguments @("-m", "compileall", "-q", "src", "panel", "run_mission.py")
    Invoke-PythonCheck -Name "dry-run" -Arguments @("run_mission.py", "tasks/e2e-smoke.json", "--dry-run")
    @{
        result = "OFFLINE_PASS";
        local_windows_live = "NOT_RUN";
        production_dependency = "jsonschema required";
        log_directory = $LogRoot;
        timestamp = (Get-Date).ToString("o")
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $LogRoot "summary.json") -Encoding UTF8
    Write-Host "[PASS] Offline checks passed. This is not a live AO Mission acceptance."
    Write-Host ("Logs: " + $LogRoot)
}
catch {
    $ExitCode = 1
    Write-Host ("[FAILED] " + $_.Exception.Message)
    if (Test-Path -LiteralPath $LogRoot) {
        $_.Exception.Message | Set-Content -LiteralPath (Join-Path $LogRoot "FAILED.txt") -Encoding UTF8
    }
}
finally {
    $env:PATH = $PreviousPath
    $env:PYTHONPATH = $PreviousPythonPath
    $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = $PreviousPlugins
    Pop-Location
}
exit $ExitCode
