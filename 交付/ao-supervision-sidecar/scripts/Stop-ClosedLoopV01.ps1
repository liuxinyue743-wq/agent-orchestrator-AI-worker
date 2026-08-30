# Stop the V0.1 closed-loop controller only (no other python killed).
# Removes the lockfile; does NOT kill AO sessions, worktrees, branches, or DB.
$lock = Join-Path $env:TEMP 'closed_loop_v01.lock'
if (Test-Path $lock) {
  Remove-Item $lock -Force -ErrorAction SilentlyContinue
  Write-Host 'lockfile removed'
} else {
  Write-Host 'no lockfile found (controller not running via Start script)'
}
# Find the python running closed_loop_cli and stop just that PID.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
  $_.CommandLine -like '*closed_loop_cli*'
} | ForEach-Object {
  Write-Host ('stopping PID ' + $_.ProcessId)
  Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
