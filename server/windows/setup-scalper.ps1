# Sets up the SCALPER on a Windows server, trading forex majors + gold with prices
# from YOUR MetaTrader 5 demo account. (Crypto is off; add it in run-scalper.ps1.)
# Paper only: fake money, no orders are ever sent to MT5.
#
# Before running: install your broker's MetaTrader 5, log in to your DEMO account,
# and in MT5 tick Tools > Options > Expert Advisors > "Allow algorithmic trading".
#
# Run in PowerShell (Run as Administrator):
#   irm https://raw.githubusercontent.com/Laxmanckl/paper-desk/main/server/windows/setup-scalper.ps1 | iex
# Safe to run again: it updates the code and restarts the scalper.

$ErrorActionPreference = "Continue"   # warnings from pip/git must not stop the script; key steps are checked below
$ProgressPreference = "SilentlyContinue"
$Repo = "Laxmanckl/paper-desk"
$Dir = "C:\paper-scalper"
$Settings = "C:\paper-scalper-settings.ps1"
$Port = 8080
$Py = "C:\Program Files\Python312\python.exe"
$Git = "C:\Program Files\Git\cmd\git.exe"
function Say($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Clean($s) { ($s -replace '\e\[20[01]~', '') -replace '[^A-Za-z0-9:_\-]', '' }
function Ask-Secret($prompt) {
  $sec = Read-Host -AsSecureString $prompt
  Clean ([Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)))
}

Say "Finding MetaTrader 5"
$terminal = Get-ChildItem "C:\Program Files\*\terminal64.exe", "C:\Program Files (x86)\*\terminal64.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $terminal) { Write-Host "MetaTrader 5 not found. Install your broker's MT5, log in to your demo account, then run this again." -ForegroundColor Yellow; return }
Write-Host "Found: $($terminal.FullName)"

Say "Installing Python 3.12 and Git (first run only)"
if (-not (Test-Path $Py)) {
  $f = "$env:TEMP\python-installer.exe"
  Invoke-WebRequest "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe" -OutFile $f
  Start-Process $f -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
}
if (-not (Test-Path $Git)) {
  $f = "$env:TEMP\git-installer.exe"
  Invoke-WebRequest "https://github.com/git-for-windows/git/releases/download/v2.47.0.windows.1/Git-2.47.0-64-bit.exe" -OutFile $f
  Start-Process $f -ArgumentList "/VERYSILENT /NORESTART" -Wait
}
if (-not (Test-Path $Py)) { Write-Host "Python did not install. Re-run this script; if it fails again, install Python 3.12 from python.org (tick 'Install for all users')." -ForegroundColor Red; return }
if (-not (Test-Path $Git)) { Write-Host "Git did not install. Re-run this script, or install Git from git-scm.com." -ForegroundColor Red; return }
& $Py -m pip install --quiet --upgrade pip
& $Py -m pip install --quiet MetaTrader5 websockets
& $Py -c "import MetaTrader5, websockets; print('MetaTrader5', MetaTrader5.__version__, '| websockets', websockets.__version__)"

Say "GitHub token (lets the scalper save its account for the dashboard)"
$cred = "$env:USERPROFILE\.git-credentials"
if (-not (Test-Path $cred)) {
  Write-Host "Paste with a RIGHT-CLICK (Ctrl+V often does nothing here), then press Enter. Nothing shows while you paste."
  $tok = ""
  for ($i = 1; $i -le 3 -and -not $tok; $i++) {
    $tok = Ask-Secret "GitHub token (github_pat_...)"
    if ($tok -and $tok.Length -lt 30) { Write-Host "Only $($tok.Length) characters arrived; a token is about 90. Try again." -ForegroundColor Yellow; $tok = "" }
    elseif (-not $tok) { Write-Host "Nothing arrived. Right-click once to paste, then press Enter." -ForegroundColor Yellow }
  }
  if (-not $tok) { Write-Host "No token entered. Run the same command again when you have it copied." -ForegroundColor Red; return }
  Write-Host "Token received ($($tok.Length) characters)." -ForegroundColor Green
  Set-Content -Path $cred -Value "https://x-access-token:$tok@github.com" -NoNewline -Encoding ascii
}
& $Git config --global credential.helper store
& $Git config --global user.name "paper-scalper-windows"
& $Git config --global user.email "paper-scalper@users.noreply.github.com"
& $Git config --global pull.rebase true

Say "Downloading the bot"
Get-ScheduledTask -TaskName "PaperScalper" -ErrorAction SilentlyContinue | Stop-ScheduledTask -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*jev_bot scalp*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
if (Test-Path "$Dir\.git") {
  & $Git -C $Dir add state/scalper_account.json
  & $Git -C $Dir commit -q -m "scalper save before update"
  & $Git -C $Dir pull -q --rebase -X theirs --autostash origin main
} else {
  & $Git clone -q "https://github.com/$Repo" $Dir
}
if (-not (Test-Path "$Dir\jev_bot")) { Write-Host "Download failed. Check the GitHub token, then run this again." -ForegroundColor Red; return }
# The installer runs as administrator but the bot runs as you: give you full
# control of the folder and tell git that's fine ("dubious ownership" otherwise).
icacls $Dir /grant "$($env:USERNAME):(OI)(CI)F" /T /Q | Out-Null
& $Git config --global --add safe.directory ($Dir -replace '\\', '/')
Push-Location $Dir
& $Py tests.py | Select-Object -Last 1
Pop-Location

if ((Test-Path $Settings) -and -not (Select-String -Path $Settings -Pattern 'MT5_LOGIN' -Quiet)) {
  Write-Host "`nYour settings have no MT5 login yet. The bot needs it to log MT5 in by itself." -ForegroundColor Yellow
  $ans = Read-Host "Enter the MT5 login details now? Type y and press Enter (or just press Enter to keep the current settings)"
  if ($ans -match '^[yY]') { Remove-Item $Settings }
}
if (-not (Test-Path $Settings)) {
  Say "Settings (stored only on this computer)"
  Write-Host "Your DEMO account details, exactly as in MT5 (File > Login to Trade Account)."
  $login = (Read-Host "MT5 demo login NUMBER, or just press the Enter key to skip").Trim()
  if ($login -and $login -notmatch '^\d+$') { Write-Host "That isn't a login number, so skipping (MT5 must already be logged in)." -ForegroundColor Yellow; $login = "" }
  $lines = @("`$env:MT5_PATH = '$($terminal.FullName)'")
  if ($login) {
    $pwSec = Read-Host -AsSecureString "MT5 demo password (right-click to paste; nothing shows)"
    $pw = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($pwSec))
    $srv = (Read-Host "MT5 server name exactly as shown in MT5 (e.g. OctaFX-Demo)").Trim()
    $pw = $pw -replace "'", "''"
    $lines += "`$env:MT5_LOGIN = '$login'", "`$env:MT5_PASSWORD = '$pw'", "`$env:MT5_SERVER = '$srv'"
  }
  $tg = Ask-Secret "Telegram bot token for alerts (right-click to paste, or just press the Enter key to skip)"
  if ($tg -and $tg -notmatch '^\d{5,}:[A-Za-z0-9_-]{30,}$') { Write-Host "That isn't a Telegram token, so skipping alerts for now." -ForegroundColor Yellow; $tg = "" }
  if ($tg) {
    $chat = Read-Host "Telegram chat id (the number from your Linux server: sudo cat /etc/paper-desk.env)"
    $lines += "`$env:TELEGRAM_BOT_TOKEN = '$tg'", "`$env:TELEGRAM_CHAT_ID = '$chat'"
  }
  $lines += "# Broker symbol names, only if auto-matching picks the wrong ones:", "# `$env:MT5_SYMBOLS = 'XAUUSD=GOLD,EURUSD=EURUSD.r'"
  Set-Content -Path $Settings -Value $lines -Encoding utf8
  icacls $Settings /inheritance:r /grant:r "$($env:USERNAME):F" "Administrators:F" | Out-Null
}

Say "Starting the scalper at every logon (MT5 needs a logged-in Windows session)"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Dir\server\windows\run-scalper.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
# Limited = same level as MT5 when you open it normally. An administrator-level
# Python cannot attach to a normally-opened MT5, so do NOT use Highest here.
Register-ScheduledTask -TaskName "PaperScalper" -Action $action -Trigger $trigger -Settings $set -RunLevel Limited -Force | Out-Null
if (-not (Get-NetFirewallRule -DisplayName "Paper scalper dashboard" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "Paper scalper dashboard" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
}
Start-ScheduledTask -TaskName "PaperScalper"
Start-Sleep 40
try {
  Start-Sleep 30
  $s = Invoke-RestMethod "http://127.0.0.1:$Port/api/state" -TimeoutSec 10
  Write-Host ("Prices for: " + (($s.prices.PSObject.Properties.Name | Sort-Object) -join ", "))
  foreach ($k in $s.feeds.PSObject.Properties.Name) { Write-Host ("  $k : " + $s.feeds.$k.status + " " + $s.feeds.$k.detail) }
} catch { Write-Host "Dashboard not answering yet." -ForegroundColor Yellow }
Write-Host "`nLast lines of the log:"
Get-Content "$Dir\scalper.log" -Tail 8 -ErrorAction SilentlyContinue

$ip = (Invoke-RestMethod "https://checkip.amazonaws.com").Trim()
Say "Done"
Write-Host @"
The scalper trades forex majors + gold and checks exits every second.
  Live dashboard:  http://localhost:$Port on this computer
                   (on a cloud server: http://${ip}:$Port after allowing TCP $Port in its firewall)
  Log file:        $Dir\scalper.log
  IMPORTANT:       close the Remote Desktop window to leave; do NOT 'Sign out' (MT5 needs the session).
  If you ever installed the crypto scalper on the Linux server, stop it there:
                   sudo systemctl disable --now paper-scalper
"@
