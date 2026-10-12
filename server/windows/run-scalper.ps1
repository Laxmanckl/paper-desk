# Keeps the scalper running: restarts it 15 seconds after any exit. Started at logon
# by the "PaperScalper" scheduled task (see setup-scalper.ps1).
#
# Watchdog: the scalper saves its account every 5 seconds. If MT5 hangs (the
# MetaTrader5 library can freeze the whole Python process), the file stops changing;
# after 5 minutes of that the scalper is killed and started again.
$Dir = "C:\paper-scalper"
$Py = "C:\Program Files\Python312\python.exe"
$Acct = "$Dir\state\scalper_account.json"
$Log = "$Dir\scalper.log"
$StaleSeconds = 300
if (Test-Path "C:\paper-scalper-settings.ps1") { . "C:\paper-scalper-settings.ps1" }
$env:PYTHONUNBUFFERED = "1"
Set-Location $Dir

function Stop-Scalper {
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'jev_bot scalp' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

while ($true) {
  "$(Get-Date -Format s) starting scalper" | Out-File -Append -Encoding utf8 $Log
  $cmd = "/c `"`"$Py`" -m jev_bot scalp --crypto none --fx majors --port 8080 --publish-every 15 --restart-on-update --account state/scalper_account.json >> `"$Log`" 2>&1`""
  $p = Start-Process -FilePath "cmd.exe" -ArgumentList $cmd -WindowStyle Hidden -PassThru
  $started = Get-Date
  while (-not $p.HasExited) {
    Start-Sleep 30
    $last = $started
    if (Test-Path $Acct) {
      $w = (Get-Item $Acct).LastWriteTime
      if ($w -gt $last) { $last = $w }
    }
    if (((Get-Date) - $last).TotalSeconds -gt $StaleSeconds) {
      "$(Get-Date -Format s) watchdog: account not saved for $StaleSeconds s (MT5 hung?); restarting the scalper" |
        Out-File -Append -Encoding utf8 $Log
      Stop-Scalper
      break
    }
  }
  Start-Sleep 15
  if ((Get-Item $Log).Length -gt 20MB) { Move-Item -Force $Log "$Dir\scalper.old.log" }
}
