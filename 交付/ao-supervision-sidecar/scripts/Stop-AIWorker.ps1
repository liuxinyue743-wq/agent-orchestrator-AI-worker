# Stop the supervision sidecar and the AO daemon. Does NOT delete projects,
# sessions, worktrees, branches, or the DB. Uses targeted PID stops only
# (no blanket taskkill). AO desktop app window is left for the user to close.
[CmdletBinding()] param()
$ErrorActionPreference = 'Stop'
$Root = Resolve-Path (Join-Path $PSScriptRoot '..' | Join-Path -ChildPath '..')
$AoDaemon = Join-Path $Root 'ao-app\resources\daemon\ao.exe'

# --- 1. Stop the supervision sidecar (only the closed_loop_cli python) ---
$lock = Join-Path $env:TEMP 'closed_loop_v01.lock'
if (Test-Path $lock) { Remove-Item $lock -Force -ErrorAction SilentlyContinue }
$sidecar = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*closed_loop_cli*' }
if ($sidecar) {
  foreach ($p in $sidecar) {
    Write-Host ('Stopping sidecar PID ' + $p.ProcessId)
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
  }
} else {
  Write-Host 'No sidecar running.'
}

# --- 2. Stop the AO daemon (graceful) ---
if (Test-Path (Join-Path $Root 'ao-data\ao.run')) {
  Write-Host 'Stopping AO daemon...'
  & $AoDaemon stop --timeout 15s 2>$null | Out-Host
} else {
  Write-Host 'No AO run file; daemon assumed stopped.'
}

# --- 3. AO desktop app: not force-killed (user closes the window). ---
Write-Host 'Done. Projects, sessions, worktrees, and the DB are preserved.'
Write-Host 'Close the AO desktop app window manually if you want it gone.'
