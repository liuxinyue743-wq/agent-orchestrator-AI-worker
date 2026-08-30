# Set AO_DATA_DIR / AO_RUN_FILE as USER-level environment variables (once),
# and migrate the AO desktop app's runtime files from the default C:\Users\<u>\.ao
# location into the E: drive data dir, so NOTHING AO-related lives on C:.
#
# The AO desktop app reads AO_DATA_DIR / AO_RUN_FILE env vars with the highest
# priority (confirmed in app.asar: ch() and $() resolver functions). So setting
# these USER-level vars and restarting the desktop app makes it use the E: drive.
#
# Idempotent. Safe to re-run. ASCII-only for the script body itself; paths use the
# Chinese parent dir but that is fine for SetEnvironmentVariable / filesystem APIs.
[CmdletBinding()] param()
$ErrorActionPreference = 'Stop'

# E:\智理杯智能体大赛\ao-data
$Root     = Resolve-Path (Join-Path $PSScriptRoot '..' | Join-Path -ChildPath '..')
$DataDir  = Join-Path $Root 'ao-data'
$RunFile  = Join-Path $DataDir 'ao.run'
$DefaultAo = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.ao'

# --- 1. USER-level env vars (the only thing allowed to live "in the environment") ---
[Environment]::SetEnvironmentVariable('AO_DATA_DIR', $DataDir, 'User')
[Environment]::SetEnvironmentVariable('AO_RUN_FILE', $RunFile, 'User')
Write-Host "Set USER-level:"
Write-Host "  AO_DATA_DIR = $DataDir"
Write-Host "  AO_RUN_FILE = $RunFile"

# --- 2. Ensure the E: data dir exists ---
if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Path $DataDir | Out-Null }

# --- 3. Migrate desktop-app runtime files from C:\Users\<u>\.ao -> E:\ao-data ---
# These are the Electron app's own runtime/config files (written at the .ao ROOT,
# not under data/). The data/ subdir (ao.db, worktrees, scratch) is kept on E:
# as-is if it already exists; we do NOT clobber an existing E: ao.db.
if (Test-Path $DefaultAo) {
    Write-Host ""
    Write-Host "Migrating desktop-app runtime files from $DefaultAo -> $DataDir"
    $rootItems = @('app-state.json','ui-settings.json','update-settings.json','daemon.log','electron')
    foreach ($name in $rootItems) {
        $s = Join-Path $DefaultAo $name
        $d = Join-Path $DataDir $name
        if (-not (Test-Path $s)) { continue }
        if (Test-Path $d) { Remove-Item $d -Recurse -Force }
        Copy-Item $s $d -Recurse -Force
        Write-Host "  migrated: $name"
    }
} else {
    Write-Host "No default .ao dir found at $DefaultAo - nothing to migrate."
}

Write-Host ""
Write-Host "Done. Now CLOSE the AO desktop app fully and relaunch it so the daemon"
Write-Host "picks up the new USER-level AO_DATA_DIR and writes to the E: drive."
