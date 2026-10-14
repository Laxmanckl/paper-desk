# Keeps the MT5 bridge running: restarts it 15 seconds after any exit.
# Started at logon by the "JevBridge" scheduled task (see setup-bridge.ps1).
#
# Watchdog: the bridge writes alive.txt after every successful push. The MetaTrader5
# library can occasionally freeze the Python process; if alive.txt stops changing
# for 5 minutes while the bridge runs, it is killed and started again.
$Dir = "C:\jev-bridge"
$Py = Get-Content "$Dir\python-path.txt" -ErrorAction SilentlyContinue
if (-not $Py) { $Py = "python" }
$Log = "$Dir\bridge.log"
$Alive = "$Dir\alive.txt"
$StaleSeconds = 300
$env:PYTHONUNBUFFERED = "1"
Set-Location $Dir

function Stop-Bridge {
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'mt5_bridge.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

while ($true) {
  if ((Test-Path $Log) -and ((Get-Item $Log).Length -gt 5MB)) { Move-Item -Force $Log "$Log.old" }
  "$(Get-Date -Format s) starting bridge" | Out-File -Append -Encoding utf8 $Log
  $cmd = "/c `"`"$Py`" `"$Dir\mt5_bridge.py`" >> `"$Log`" 2>&1`""
  $p = Start-Process -FilePath "cmd.exe" -ArgumentList $cmd -WindowStyle Hidden -PassThru
  $started = Get-Date
  while (-not $p.HasExited) {
    Start-Sleep 30
    $last = $started
    if (Test-Path $Alive) { $w = (Get-Item $Alive).LastWriteTime; if ($w -gt $last) { $last = $w } }
    if (((Get-Date) - $last).TotalSeconds -gt $StaleSeconds) {
      "$(Get-Date -Format s) no successful push for 5 min; restarting bridge" | Out-File -Append -Encoding utf8 $Log
      Stop-Bridge
      break
    }
  }
  Start-Sleep 15
}
