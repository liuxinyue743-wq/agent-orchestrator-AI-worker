$bytes = [System.IO.File]::ReadAllBytes('E:\智理杯智能体大赛\ao-app\resources\app.asar')
$text = [System.Text.Encoding]::UTF8.GetString($bytes)
$patterns = @('AO_DATA_DIR','\.ao','appData','getPath','userData','running.json','dataDir','data-dir','AO_RUN_FILE','XDG')
foreach ($p in $patterns) {
    $idx = $text.IndexOf($p)
    Write-Host ("{0,-15} first@{1}" -f $p, $idx)
}
