#!/usr/bin/env bash
# Turns a fresh Ubuntu server into the always-on paper-trading bot.
#
#   curl -fsSL https://raw.githubusercontent.com/Laxmanckl/paper-desk/main/server/setup.sh | bash
#
# What it does:
#   1. installs git + python3
#   2. asks for your GitHub token (so the server can save the account to GitHub)
#   3. downloads the bot, runs its self-tests
#   4. switches the repo to server mode (state/runner.txt = server), so GitHub stops trading
#   5. installs a background service that checks prices every minute, restarts itself
#      if it crashes or the server reboots, and pushes the account every 15 minutes
#
# Safe to run again: it updates the code and restarts the service.
# Paper trading only: there is no broker code, so it cannot place a real trade.
set -euo pipefail

REPO="${REPO:-Laxmanckl/paper-desk}"
DIR="$HOME/paper-desk"
SERVICE=paper-desk

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "Installing git and python3"
sudo apt-get update -qq
sudo apt-get install -y -qq git python3 >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3, 10), "need Python 3.10+"; print("python", sys.version.split()[0])'

if [ ! -s "$HOME/.git-credentials" ]; then
  say "GitHub token"
  echo "Paste the token you created on GitHub (starts with github_pat_)."
  echo "Nothing will show while you paste. Press Enter after."
  read -rsp "Token: " TOKEN </dev/tty; echo
  [ -n "$TOKEN" ] || { echo "No token entered."; exit 1; }
  printf 'https://x-access-token:%s@github.com\n' "$TOKEN" > "$HOME/.git-credentials"
  chmod 600 "$HOME/.git-credentials"
fi
git config --global credential.helper store
git config --global user.name  "paper-desk-server"
git config --global user.email "paper-desk-server@users.noreply.github.com"
git config --global pull.rebase true

say "Downloading the bot"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" add state/paper_account.json 2>/dev/null || true
  git -C "$DIR" diff --cached --quiet || git -C "$DIR" commit -q -m "server check before update"
  git -C "$DIR" pull -q --rebase -X theirs --autostash origin main
else
  git clone -q "https://github.com/$REPO" "$DIR"
fi
cd "$DIR"

say "Running self-tests"
python3 tests.py | tail -1

say "Switching GitHub to server mode"
sudo systemctl stop "$SERVICE" 2>/dev/null || true
# keep the account this server has been trading (it changes every minute), then
# take the newest code; if both changed the account, the server's copy wins
git add state/paper_account.json 2>/dev/null || true
git diff --cached --quiet || git commit -q -m "server check before update"
git pull -q --rebase -X theirs --autostash origin main
if [ "$(cat state/runner.txt 2>/dev/null | tr -d '[:space:]')" != "server" ]; then
  echo server > state/runner.txt
  git add state/runner.txt
  git commit -q -m "Switch to always-on server"
fi
if ! git push -q origin HEAD:main; then
  echo
  echo "Could not push to GitHub. The token is probably wrong or lacks 'Contents: Read and write'"
  echo "on the $REPO repository. Make a new token, then run:"
  echo "    rm ~/.git-credentials   and run this setup again."
  exit 1
fi
echo "GitHub will now only rebuild the dashboard; this server does the trading."

say "Installing the background service"
sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<UNIT
[Unit]
Description=Paper-trading bot (gold + EUR/USD, fake money)
After=network-online.target
Wants=network-online.target

[Service]
User=${USER:-$(id -un)}
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-/etc/paper-desk.env
ExecStart=/usr/bin/python3 -m jev_bot live --watch 1 --runner server --publish-every 15 --account state/paper_account.json
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE" >/dev/null 2>&1
sleep 20
sudo systemctl --no-pager --lines=0 status "$SERVICE" | sed -n 1,3p

say "Done"
cat <<MSG
The bot now checks prices every minute, around the clock.
  Dashboard:     https://$(echo "${REPO%%/*}" | tr 'A-Z' 'a-z').github.io/${REPO##*/}/   (updates every ~15 min)
  Live log:      journalctl -u $SERVICE -f        (Ctrl+C to stop watching)
  Status:        sudo systemctl status $SERVICE
  Update code:   run this setup command again
  Hand back to GitHub:  bash ~/paper-desk/server/stop.sh
MSG
