#!/usr/bin/env bash
# Turns your Lightsail server into the always-on Jev Desk.
#
#   curl -fsSL https://raw.githubusercontent.com/Laxmanckl/paper-desk/main/server/setup.sh | bash
#
# What it does:
#   1. retires the old Paper Desk services (paper-desk, paper-scalper) if they exist
#   2. reuses the GitHub token already stored on this server (asks for one if missing)
#   3. updates ~/paper-desk to the Jev Desk code and runs the tests
#   4. installs the jev-desk service: the 24/7 block loop, a live page on port 8080
#      that updates every second, and a push to GitHub every 15 minutes
#
# Safe to run again: it updates the code and restarts the service.
# Paper only: no broker or wallet code exists, so it cannot place a real order.
# The old bot is kept on the archive/paper-desk branch on GitHub.
set -euo pipefail

REPO="${REPO:-Laxmanckl/paper-desk}"
DIR="$HOME/paper-desk"
SERVICE=jev-desk
PORT=8080
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "Installing git, python3 and pytest"
sudo apt-get update -qq
sudo apt-get install -y -qq git python3 python3-pytest >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3, 10), "need Python 3.10+"; print("python", sys.version.split()[0])'

say "Retiring the old Paper Desk services"
for old in paper-desk paper-scalper; do
  if systemctl list-unit-files "$old.service" >/dev/null 2>&1 && [ -f "/etc/systemd/system/$old.service" ]; then
    sudo systemctl disable --now "$old" >/dev/null 2>&1 || true
    sudo rm -f "/etc/systemd/system/$old.service"
    echo "stopped and removed $old"
  fi
done
sudo systemctl daemon-reload

if [ ! -s "$HOME/.git-credentials" ]; then
  say "GitHub token"
  echo "Paste a GitHub token with 'Contents: Read and write' on $REPO (starts with github_pat_)."
  echo "Nothing will show while you paste. Press Enter after."
  read -rsp "Token: " RAW </dev/tty; echo
  RAW=${RAW//$'\e[200~'/}; RAW=${RAW//$'\e[201~'/}
  TOKEN=$(printf '%s' "$RAW" | tr -cd 'A-Za-z0-9_')
  [ -n "$TOKEN" ] || { echo "No token entered."; exit 1; }
  printf 'https://x-access-token:%s@github.com\n' "$TOKEN" > "$HOME/.git-credentials"
  chmod 600 "$HOME/.git-credentials"
fi
git config --global credential.helper store
git config --global user.name  "jev-desk-server"
git config --global user.email "jev-desk-server@users.noreply.github.com"
git config --global pull.rebase true

say "Getting the Jev Desk code"
sudo systemctl stop "$SERVICE" 2>/dev/null || true
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" add state/jev_desk.json 2>/dev/null || true
  git -C "$DIR" diff --cached --quiet 2>/dev/null || git -C "$DIR" commit -q -m "jev desk save before update" || true
  git -C "$DIR" fetch -q origin main
  if git -C "$DIR" cat-file -e origin/main:jevsys/__init__.py 2>/dev/null && [ -d "$DIR/jevsys" ]; then
    git -C "$DIR" pull -q --rebase -X theirs --autostash origin main
  else
    # first switch from the old Paper Desk: take the new code as-is
    git -C "$DIR" reset -q --hard origin/main
    git -C "$DIR" clean -fdq -e logs/
  fi
else
  git clone -q "https://github.com/$REPO" "$DIR"
fi
cd "$DIR"

# old processes may have pushed their account files after the switch; tidy them away
LEFT=$(git ls-files state/paper_account.json state/scalper_account.json state/scalper_orb.json state/runner.txt)
if [ -n "$LEFT" ]; then
  git rm -q $LEFT
  git commit -q -m "Remove old Paper Desk state files"
fi
if ! git push -q origin HEAD:main; then
  echo
  echo "Could not push to GitHub. The token is probably wrong or lacks 'Contents: Read and write'"
  echo "on $REPO. Make a new token, then run:  rm ~/.git-credentials   and run this setup again."
  exit 1
fi

say "Running tests"
python3 -m pytest -q tests | tail -1

say "Installing the jev-desk service"
sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<UNIT
[Unit]
Description=Jev Desk (24/7 block loop, paper only)
After=network-online.target
Wants=network-online.target

[Service]
User=${USER:-$(id -un)}
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-/etc/jev-desk.env
ExecStart=/usr/bin/python3 -m jevsys run --runner server --port $PORT --publish-every 15 --state state/jev_desk.json --logs logs
Restart=always
RestartSec=10
MemoryMax=400M

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE" >/dev/null 2>&1
sleep 8
sudo systemctl --no-pager --lines=0 status "$SERVICE" | sed -n 1,3p
if curl -fsS "http://127.0.0.1:$PORT/api/state" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("blocks so far:", d["totals"]["blocks_session"], "| snapshot ~", d["engine"]["tokens"], "tokens")'; then :; else
  echo "The live page isn't answering yet. Check:  journalctl -u $SERVICE -n 50"
fi

IP=$(curl -fsS https://checkip.amazonaws.com 2>/dev/null || echo "<your-server-ip>")
OWNER=$(echo "${REPO%%/*}" | tr 'A-Z' 'a-z')
say "Done"
cat <<MSG
The Jev Desk is running: one snapshot every 300 ms block, around the clock.

  Live page:     http://$IP:$PORT   (updates every second; port $PORT is the one
                 you already opened in Lightsail for the scalper)
  Dashboard:     https://$OWNER.github.io/${REPO##*/}/   (updates every ~15 min)
  Live log:      journalctl -u $SERVICE -f
  Status:        sudo systemctl status $SERVICE
  Update code:   run this setup command again
  Stop:          bash ~/paper-desk/server/stop.sh
MSG
