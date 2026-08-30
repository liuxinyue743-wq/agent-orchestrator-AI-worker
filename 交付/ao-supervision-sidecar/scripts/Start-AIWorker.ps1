# Start the whole system: AO desktop app + daemon + supervision sidecar.
# Double-click Start-AIWorker.cmd (which calls this). Safe to re-run:
# never starts a second AO or a second sidecar.
[CmdletBinding()] param(
  [string]$Task = ''
)
$ErrorActionPreference = 'Stop'
$Root = Resolve-Path (Join-Path $PSScriptRoot '..' | Join-Path -ChildPath '..')
$DataDir = Join-Path $Root 'ao-data'
$RunFile = Join-Path $DataDir 'ao.run'
$AoApp = Join-Path $Root 'ao-app\agent-orchestrator.exe'
$AoDaemon = Join-Path $Root 'ao-app\resources\daemon\ao.exe'
$SidecarRoot = Join-Path $Root 'ao-supervision-sidecar'

# Ensure the current process has the env vars (works even before the
# user-level vars from Setup-Environment.ps1 are picked up by new shells).
$env:AO_DATA_DIR = $DataDir
$env:AO_RUN_FILE = $RunFile

# --- 1. AO desktop app (start if not running) ---
$aoProc = Get-Process -Name 'agent-orchestrator' -ErrorAction SilentlyContinue
if (-not $aoProc) {
  Write-Host 'Starting AO desktop app...'
  Start-Process -FilePath $AoApp
} else {
  Write-Host 'AO desktop app already running.'
}

# --- 2. Wait for daemon to be reachable (it is started by the desktop app) ---
Write-Host 'Waiting for AO daemon...'
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
  Start-Sleep -Seconds 2
  if (Test-Path $RunFile) {
    $st = & $AoDaemon status 2>$null
    if ($LASTEXITCODE -eq 0 -and ($st -join ' ') -match 'ready') { $ready = $true; break }
  }
}
if (-not $ready) { Write-Error 'AO daemon did not become ready in 120s. Run ao doctor.'; exit 1 }
Write-Host 'AO daemon ready.'

# --- 3. Prevent double sidecar via lockfile ---
$lock = Join-Path $env:TEMP 'closed_loop_v01.lock'
if (Test-Path $lock) {
  # Stale lock? Check if a python closed_loop_cli is actually running.
  $running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*closed_loop_cli*' }
  if ($running) { Write-Host 'Supervision sidecar already running. Nothing to do.'; exit 0 }
  Remove-Item $lock -Force -ErrorAction SilentlyContinue
}
New-Item -ItemType File -Path $lock -Force | Out-Null

# --- 4. Start sidecar (foreground watch by default) ---
$taskArg = if ($Task) { $Task } else {
  Join-Path $SidecarRoot 'tasks\demo-repeated-error.json'
}
Write-Host "Starting supervision sidecar (task: $taskArg)..."
Write-Host 'Ctrl+C to stop the sidecar only (AO keeps running).'
try {
  Push-Location $SidecarRoot
  python -m src.closed_loop_cli --task $taskArg --watch
} finally {
  Remove-Item $lock -ErrorAction SilentlyContinue
  Pop-Location
}
