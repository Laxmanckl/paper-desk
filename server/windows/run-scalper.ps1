# Keeps the scalper running: restarts it 15 seconds after any exit. Started at logon
# by the "PaperScalper" scheduled task (see setup-scalper.ps1).
$Dir = "C:\paper-scalper"
$Py = "C:\Program Files\Python312\python.exe"
if (Test-Path "C:\paper-scalper-settings.ps1") { . "C:\paper-scalper-settings.ps1" }
$env:PYTHONUNBUFFERED = "1"
Set-Location $Dir
while ($true) {
  "$(Get-Date -Format s) starting scalper" | Out-File -Append -Encoding utf8 "$Dir\scalper.log"
  & $Py -m jev_bot scalp --crypto majors --fx majors --port 8080 --publish-every 15 --account state/scalper_account.json *>> "$Dir\scalper.log"
  Start-Sleep 15
  if ((Get-Item "$Dir\scalper.log").Length -gt 20MB) { Move-Item -Force "$Dir\scalper.log" "$Dir\scalper.old.log" }
}
