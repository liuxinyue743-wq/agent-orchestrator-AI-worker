# Start the V0.1 closed-loop controller. ASCII-only; paths from $PSScriptRoot.
[CmdletBinding()] param(
  [string]$Task = (Join-Path $PSScriptRoot '..' | Join-Path -ChildPath 'tasks' | Join-Path -ChildPath 'demo-repeated-error.json')
)
$ErrorActionPreference = 'Stop'
$Root = Resolve-Path (Join-Path $PSScriptRoot '..' | Join-Path -ChildPath '..')
$env:AO_DATA_DIR = Join-Path $Root 'ao-data'
$env:AO_RUN_FILE = Join-Path $Root 'ao-data\ao.run'
$ao = Join-Path $Root 'ao-app\resources\daemon\ao.exe'
& $ao status | Out-Host
# prevent double start: simple lockfile
$lock = Join-Path $env:TEMP 'closed_loop_v01.lock'
if (Test-Path $lock) { Write-Error 'Controller already running (lockfile exists)'; exit 1 }
New-Item -ItemType File -Path $lock -Force | Out-Null
try {
  Push-Location (Join-Path $PSScriptRoot '..')
  python -m src.closed_loop_cli --task $Task --watch
} finally {
  Remove-Item $lock -ErrorAction SilentlyContinue
  Pop-Location
}
