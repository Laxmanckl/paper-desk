# Stops the old MT5 scalper on the Windows PC for good.
# Run in PowerShell:  powershell -ExecutionPolicy Bypass -File retire-scalper.ps1
# (or paste the lines below). The code stays in C:\paper-scalper; the old bot is
# kept on GitHub's archive/paper-desk branch.
Get-ScheduledTask -TaskName "PaperScalper" -ErrorAction SilentlyContinue | Stop-ScheduledTask -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "PaperScalper" -Confirm:$false -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'jev_bot scalp' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { $_.CommandLine -match 'run-scalper' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host "Old scalper stopped and removed from startup."
