# Setup the closed-loop demo repo. Run explicitly; safe to re-run (reuses).
# ASCII-only; derive paths from $PSScriptRoot to avoid GBK mojibake on PS 5.1.
[CmdletBinding()] param(
  [string]$Root = (Join-Path $PSScriptRoot '..' | Join-Path -ChildPath '..')
)
$ErrorActionPreference = 'Stop'
$Demo = Join-Path $Root 'closed-loop-demo'
$Origin = Join-Path $Root 'closed-loop-demo-origin.git'
$Template = Join-Path $PSScriptRoot '..' | Join-Path -ChildPath 'demo' | Join-Path -ChildPath 'template'

if (-not (Test-Path $Demo)) {
  New-Item -ItemType Directory -Force -Path $Demo | Out-Null
  Copy-Item -Recurse -Force (Join-Path $Template '*') $Demo
}

Set-Location $Demo
if (-not (Test-Path (Join-Path $Demo '.git'))) {
  git init -q
  git config user.name 'demo'
  git config user.email 'demo@local'
  git add -A
  git commit -q -m 'initial: app.py with add(); failing divide tests'
}

if (-not (Test-Path $Origin)) {
  git clone --bare -q $Demo $Origin
}
$originArg = (Resolve-Path $Origin).Path -replace '\\','/'
git remote remove origin 2>$null
git remote add origin $originArg
git push -u origin master 2>$null
git remote set-head origin master 2>$null

# Register AO project (reuse if exists)
$ao = Join-Path $Root 'ao-app\resources\daemon\ao.exe'
& $ao project add --id closed-loop-demo --name closed-loop-demo --path $Demo --worker-agent claude-code 2>$null
Write-Host "demo repo ready at $Demo"
Write-Host "origin at $Origin"
