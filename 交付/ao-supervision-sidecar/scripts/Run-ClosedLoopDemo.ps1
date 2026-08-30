# One-shot demo: setup repo -> spawn worker -> auto-bind session -> run closed loop to DONE.
[CmdletBinding()] param()
$ErrorActionPreference = 'Stop'
$Root = Resolve-Path (Join-Path $PSScriptRoot '..' | Join-Path -ChildPath '..')
$env:AO_DATA_DIR = Join-Path $Root 'ao-data'
$env:AO_RUN_FILE = Join-Path $Root 'ao-data\ao.run'
$ao = Join-Path $Root 'ao-app\resources\daemon\ao.exe'
$TaskFile = Join-Path $PSScriptRoot '..' | Join-Path -ChildPath 'tasks' | Join-Path -ChildPath 'demo-repeated-error.json'

# 1. ensure daemon
& $ao status | Out-Null

# 2. setup demo repo (idempotent)
& (Join-Path $PSScriptRoot 'setup_demo_repo.ps1') | Out-Host

# 3. spawn worker that runs failing tests 3x (creates real REPEATED_ERROR)
$spawn = & $ao spawn --project closed-loop-demo --harness claude-code --name demo-worker `
  --mode chat --prompt "Read app.py and tests/test_divide.py. Then run 'python -m pytest -q' three times back-to-back. Do NOT modify code. Reply DONE_RUNNING when finished."
$spawn | Out-Host
$sid = $null
if ($spawn -match 'spawned session (\S+)') { $sid = $Matches[1] }
if (-not $sid) { throw "could not parse worker session id from: $spawn" }
Write-Host "bound worker_session_id = $sid"

# 4. wait for worker to produce error activities
Start-Sleep -Seconds 90

# 5. drive the closed loop to DONE (real auditor+planner+ao send+gate).
#    --watch polls until a terminal state (DONE/HUMAN/FAILED) is reached.
Push-Location (Join-Path $PSScriptRoot '..')
python -m src.closed_loop_cli --task $TaskFile --worker-session $sid --watch | Out-Host
Pop-Location
