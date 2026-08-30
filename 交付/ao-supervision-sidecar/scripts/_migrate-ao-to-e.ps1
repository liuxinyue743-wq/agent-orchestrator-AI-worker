$ErrorActionPreference = 'Stop'
$src = 'C:\Users\lenovo\.ao'
$dst = 'E:\智理杯智能体大赛\ao-data'

# Files/dirs at the ROOT of ~/.ao that the desktop app writes (not under data/).
# These are runtime config the Electron app needs; E: target currently lacks them.
$rootItems = @('app-state.json','ui-settings.json','update-settings.json','daemon.log','electron')

Write-Host "Migrating desktop-app runtime files: $src -> $dst"
foreach ($name in $rootItems) {
    $s = Join-Path $src $name
    $d = Join-Path $dst $name
    if (-not (Test-Path $s)) { Write-Host "  SKIP (missing in source): $name"; continue }
    if (Test-Path $d) {
        Write-Host "  OVERWRITE existing: $name"
        Remove-Item $d -Recurse -Force
    } else {
        Write-Host "  COPY new: $name"
    }
    Copy-Item $s $d -Recurse -Force
}
Write-Host "Done."
Write-Host ""
Write-Host "=== E:\ao-data root after migration ==="
Get-ChildItem $dst -Force | Select-Object Mode,Length,Name | Format-Table -AutoSize
