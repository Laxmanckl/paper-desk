# Sets up the MT5 price bridge on this Windows PC.
#
# Before running: MetaTrader 5 is installed, open, and logged in to your demo account.
# Run in PowerShell (normal, not administrator):
#   irm https://raw.githubusercontent.com/Laxmanckl/paper-desk/main/bridge/setup-bridge.ps1 -OutFile $env:TEMP\setup-bridge.ps1
#   powershell -ExecutionPolicy Bypass -File $env:TEMP\setup-bridge.ps1
#
# It READS prices from MT5 and sends them to your Lightsail server. It never trades.
# Safe to run again: it updates the bridge and restarts it.
$ErrorActionPreference = "Stop"
$Repo = "https://raw.githubusercontent.com/Laxmanckl/paper-desk/main/bridge"
$Dir = "C:\jev-bridge"
function Say($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }

Say "Finding Python"
$Py = $null
foreach ($c in @("C:\Program Files\Python312\python.exe", "C:\Program Files\Python313\python.exe", "C:\Program Files\Python311\python.exe")) {
  if (Test-Path $c) { $Py = $c; break }
}
if (-not $Py) { $cmd = Get-Command python -ErrorAction SilentlyContinue; if ($cmd -and $cmd.Source -notmatch "WindowsApps") { $Py = $cmd.Source } }
if (-not $Py) {
  Write-Host "Python not found. Install Python 3.12 from https://www.python.org/downloads/windows/"
  Write-Host "(tick 'Add python.exe to PATH'), then run this setup again."
  exit 1
}
& $Py --version

Say "Installing the MetaTrader5 library"
& $Py -m pip install --quiet --upgrade MetaTrader5
if ($LASTEXITCODE -ne 0) { Write-Host "pip failed. Check your internet connection and run again."; exit 1 }

Say "Stopping any running bridge"
Get-ScheduledTask -TaskName "JevBridge" -ErrorAction SilentlyContinue | Stop-ScheduledTask -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'mt5_bridge.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -match 'run-bridge' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Say "Downloading the bridge to $Dir"
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
Invoke-WebRequest "$Repo/mt5_bridge.py" -OutFile "$Dir\mt5_bridge.py" -UseBasicParsing
Invoke-WebRequest "$Repo/run-bridge.ps1" -OutFile "$Dir\run-bridge.ps1" -UseBasicParsing
Set-Content -Path "$Dir\python-path.txt" -Value $Py -Encoding ascii

$Settings = "$Dir\settings.json"
if (-not (Test-Path $Settings)) {
  Say "Connect to your server"
  Write-Host "Both values were printed at the end of the server setup (server/setup.sh)."
  $Server = (Read-Host "Server address (like 3.110.20.30:8080)").Trim()
  $Token = (Read-Host "Bridge token").Trim()
  if (-not $Server -or -not $Token) { Write-Host "Both are needed. Run this setup again."; exit 1 }
  if ($Server -notmatch '^https?://') { $Server = "http://$Server" }
  @{ server = $Server; token = $Token; interval_s = 10; mt5_path = ""; symbols = @{} } |
    ConvertTo-Json | Set-Content -Path $Settings -Encoding utf8
} else {
  Write-Host "Keeping existing settings in $Settings"
}

Say "Testing one push (MT5 must be open and logged in)"
& $Py "$Dir\mt5_bridge.py" --once
if ($LASTEXITCODE -ne 0) {
  Write-Host ""
  Write-Host "The test push failed. Check the message above:"
  Write-Host " - 'MT5 not reachable': open MetaTrader 5, log in, and run this again."
  Write-Host " - 'wrong token': delete $Settings and run this again."
  Write-Host " - timed out / refused: check the server address, and that port 8080 is open in Lightsail."
  exit 1
}

Say "Starting the bridge at every logon"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Dir\run-bridge.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "JevBridge" -Action $action -Trigger $trigger -Settings $set -RunLevel Limited -Force | Out-Null
Start-ScheduledTask -TaskName "JevBridge"

Say "Keeping the PC awake while plugged in"
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0

Say "Done"
Write-Host "The bridge sends gold + forex prices to your server every 10 seconds."
Write-Host "  Log:        Get-Content $Dir\bridge.log -Tail 20 -Wait"
Write-Host "  Stop it:    Unregister-ScheduledTask JevBridge -Confirm:`$false   (then restart the PC)"
Write-Host "Keep MetaTrader 5 open and logged in. The PC must stay on while the market is open."
