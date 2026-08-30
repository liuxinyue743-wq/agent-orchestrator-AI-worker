[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $AuditorSession,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $PlannerSession,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $WorkerSession,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $GateRepo,

    [string] $RunId,

    [ValidateSet("Pass", "Fail", "All")]
    [string] $Scenario = "All"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

function Assert-DemoCondition {
    param(
        [Parameter(Mandatory = $true)]
        [bool] $Condition,

        [Parameter(Mandatory = $true)]
        [string] $Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

function ConvertTo-WindowsCommandLineArgument {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string] $Argument
    )

    # ProcessStartInfo.Arguments is a single Windows command-line string on
    # Windows PowerShell 5.1. Quote every argv item using the Windows CRT
    # backslash rules so embedded JSON quotes and whitespace survive together.
    $escaped = [regex]::Replace($Argument, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-DemoGate {
    param(
        [Parameter(Mandatory = $true)]
        [string] $AuditId,

        [Parameter(Mandatory = $true)]
        [object[]] $Commands
    )

    $arguments = @(
        "--gate",
        "--auditor-session", $AuditorSession,
        "--planner-session", $PlannerSession,
        "--worker-session", $WorkerSession,
        "--audit-id", $AuditId,
        "--task-goal", "Demonstrate deterministic Integration Gate feedback without modifying product files.",
        "--acceptance-criterion", "A passing Gate returns gate.passed=true and no audited result.",
        "--acceptance-criterion", "The Auditor recommends LOCAL_FIX only when a Gate step has stdout exactly DEMO-GATE-FAIL and exit_code=7.",
        "--acceptance-criterion", "For any other unexpected Gate failure, the Auditor recommends HUMAN and no Worker feedback is sent.",
        "--acceptance-criterion", "For LOCAL_FIX, the Worker returns DEMO-GATE-ACK followed by the gate auditId.",
        "--constraint", "Do not modify files or create commits.",
        "--constraint", "Do not create, stop, resume, delegate, or schedule AO sessions.",
        "--constraint", "Do not run merge, checkout, reset, commit, or push.",
        "--evidence", "This is a controlled competition demonstration of the existing Integration Gate.",
        "--gate-repo", $script:ResolvedGateRepo
    )
    foreach ($command in $Commands) {
        $arguments += "--gate-command-json"
        $commandJson = ConvertTo-Json -InputObject ([string[]] $command) -Compress
        $arguments += $commandJson
    }

    $nativeArguments = $arguments | ForEach-Object {
        ConvertTo-WindowsCommandLineArgument ([string] $_)
    }
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $script:ClaoPath
    $startInfo.Arguments = [string]::Join(" ", [string[]] $nativeArguments)
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $startInfo.StandardOutputEncoding = $utf8
    $startInfo.StandardErrorEncoding = $utf8

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        [void] $process.Start()
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $exitCode = $process.ExitCode
        $stdout = $stdoutTask.Result.Trim()
        $stderr = $stderrTask.Result.Trim()
    }
    finally {
        $process.Dispose()
    }

    Assert-DemoCondition (-not [string]::IsNullOrWhiteSpace($stdout)) (
        "clao produced no JSON stdout (exit $exitCode)."
    )
    try {
        $payload = $stdout | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "clao stdout was not a single JSON object (exit $exitCode)."
    }
    Assert-DemoCondition ($payload -is [pscustomobject]) (
        "clao stdout JSON was not an object (exit $exitCode)."
    )
    $errorProperty = $payload.PSObject.Properties["error"]
    if ($null -ne $errorProperty -and $null -ne $errorProperty.Value) {
        $errorJson = ConvertTo-Json -InputObject $errorProperty.Value -Compress
        throw "clao returned error JSON (exit $exitCode): $errorJson"
    }
    Assert-DemoCondition ([string]::IsNullOrWhiteSpace($stderr)) (
        "clao wrote unexpected stderr (exit $exitCode)."
    )

    return [pscustomobject]@{
        ExitCode = $exitCode
        Payload = $payload
    }
}

function Invoke-PassScenario {
    Write-Host "[demo] Pass: running pytest, compileall, and pip check"
    $commands = @(
        ,([string[]] @($script:GatePython, "-m", "pytest"))
        ,([string[]] @($script:GatePython, "-m", "compileall", "-q", "src"))
        ,([string[]] @($script:GatePython, "-m", "pip", "check"))
    )
    $run = Invoke-DemoGate -AuditId $script:EffectiveRunId -Commands $commands

    Assert-DemoCondition ($run.ExitCode -eq 0) (
        "Pass expected clao exit 0, got $($run.ExitCode)."
    )
    Assert-DemoCondition ($run.Payload.gate.passed -eq $true) (
        "Pass expected gate.passed=true."
    )
    Assert-DemoCondition ($null -eq $run.Payload.auditedResult) (
        "Pass expected auditedResult=null."
    )

    Write-Host "[demo] Pass: OK (exit=0, gate.passed=true, auditedResult=null)"
}

function Invoke-FailScenario {
    Write-Host "[demo] Fail: running controlled exit 7 command"
    $commands = @(
        ,([string[]] @(
            $script:GatePython,
            "-c",
            "import sys; print('DEMO-GATE-FAIL'); sys.exit(7)"
        ))
    )
    $run = Invoke-DemoGate -AuditId $script:EffectiveRunId -Commands $commands

    Assert-DemoCondition ($run.ExitCode -eq 3) (
        "Fail expected clao exit 3, got $($run.ExitCode)."
    )
    Assert-DemoCondition ($run.Payload.gate.passed -eq $false) (
        "Fail expected gate.passed=false."
    )
    $steps = @($run.Payload.gate.steps)
    Assert-DemoCondition ($steps.Count -eq 1) (
        "Fail expected exactly one Gate step."
    )
    Assert-DemoCondition ($steps[0].exit_code -eq 7) (
        "Fail expected Gate step exit 7."
    )
    Assert-DemoCondition ($steps[0].stdout.Trim() -eq "DEMO-GATE-FAIL") (
        "Fail expected Gate stdout DEMO-GATE-FAIL."
    )

    $gateAuditId = [string] $run.Payload.gateAuditId
    Assert-DemoCondition (-not [string]::IsNullOrWhiteSpace($gateAuditId)) (
        "Fail expected a non-empty gateAuditId."
    )
    Assert-DemoCondition ($run.Payload.auditedResult.decision -eq "LOCAL_FIX") (
        "Fail expected PlannerDecision LOCAL_FIX."
    )
    $expectedWorkerResponse = "DEMO-GATE-ACK $gateAuditId"
    Assert-DemoCondition (
        $run.Payload.auditedResult.workerResponse -eq $expectedWorkerResponse
    ) (
        "Fail expected Worker response '$expectedWorkerResponse'."
    )

    Write-Host (
        "[demo] Fail: OK (exit=3, decision=LOCAL_FIX, " +
        "worker=DEMO-GATE-ACK $gateAuditId)"
    )
}

try {
    $repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
    $script:ResolvedGateRepo = (Resolve-Path -LiteralPath $GateRepo).Path
    Assert-DemoCondition (
        Test-Path -LiteralPath $script:ResolvedGateRepo -PathType Container
    ) "GateRepo must be an existing directory."

    $script:ClaoPath = Join-Path $repoRoot ".venv\Scripts\clao.exe"
    Assert-DemoCondition (Test-Path -LiteralPath $script:ClaoPath -PathType Leaf) (
        "Missing repo CLI: .venv\Scripts\clao.exe"
    )
    $script:GatePython = Join-Path $script:ResolvedGateRepo ".venv\Scripts\python.exe"
    Assert-DemoCondition (Test-Path -LiteralPath $script:GatePython -PathType Leaf) (
        "Missing GateRepo Python: .venv\Scripts\python.exe"
    )

    if ([string]::IsNullOrWhiteSpace($RunId)) {
        $timestamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
        $suffix = [guid]::NewGuid().ToString("N").Substring(0, 8)
        $script:EffectiveRunId = "competition-demo-$timestamp-$suffix"
    }
    else {
        $script:EffectiveRunId = $RunId.Trim()
    }

    Write-Host "[demo] RunId: $script:EffectiveRunId"
    if ($Scenario -eq "Pass" -or $Scenario -eq "All") {
        Invoke-PassScenario
    }
    if ($Scenario -eq "Fail" -or $Scenario -eq "All") {
        Invoke-FailScenario
    }
    Write-Host "[demo] Complete"
}
catch {
    [Console]::Error.WriteLine("[demo] ERROR: $($_.Exception.Message)")
    exit 1
}
