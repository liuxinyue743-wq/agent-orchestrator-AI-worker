Write-Host '=== opencode binary? ==='
$c = Get-Command opencode -ErrorAction SilentlyContinue
if ($c) { Write-Host ("  found: " + $c.Source) } else { Write-Host '  opencode NOT in PATH' }

Write-Host '=== opencode config dirs ==='
foreach ($p in @(
  (Join-Path $env:USERPROFILE '.config\opencode'),
  (Join-Path $env:USERPROFILE '.opencode'),
  (Join-Path $env:APPDATA 'opencode'),
  (Join-Path $env:LOCALAPPDATA 'opencode')
)) { Write-Host ("  " + $p + " exists=" + (Test-Path $p)) }

Write-Host '=== kimi / moonshot ==='
$kc = Get-Command kimi -ErrorAction SilentlyContinue
if ($kc) { Write-Host ("  kimi: " + $kc.Source) } else { Write-Host '  kimi NOT in PATH' }

Write-Host '=== relevant env (User scope) ==='
foreach ($k in @('MOONSHOT_API_KEY','KIMI_API_KEY','OPENAI_API_KEY','ANTHROPIC_BASE_URL','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_API_KEY','OPENAI_BASE_URL','GLM_API_KEY','ZHIPUAI_API_KEY','OPENCODE_CONFIG')) {
  $v = [Environment]::GetEnvironmentVariable($k,'User')
  if ($v) { Write-Host ("  " + $k + " = (set, len " + $v.Length + ")") } else { Write-Host ("  " + $k + " = (empty)") }
}
