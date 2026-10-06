#!/usr/bin/env bash
# Adds the SCALPER (1-minute signals, exits checked every second) to this server,
# trading crypto majors on live Binance prices. Paper only: fake money.
#
#   curl -fsSL https://raw.githubusercontent.com/Laxmanckl/paper-desk/main/server/scalper-setup.sh | bash
#
# Runs next to the daily bot as its own service (paper-scalper), from its own copy
# of the code (~/paper-scalper), so the two never get in each other's way.
# Safe to run again: it updates the code and restarts the scalper.
set -euo pipefail
REPO="${REPO:-Laxmanckl/paper-desk}"
DIR="$HOME/paper-scalper"
SERVICE=paper-scalper
PORT=8080
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[ -s "$HOME/.git-credentials" ] || { echo "Run the main setup first (server/setup.sh): it stores the GitHub token."; exit 1; }

say "Installing the live-price library"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-websockets >/dev/null
python3 -c 'import websockets; print("websockets", websockets.__version__)'

say "Downloading the bot (separate copy for the scalper)"
sudo systemctl stop "$SERVICE" 2>/dev/null || true
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" add state/scalper_account.json 2>/dev/null || true
  git -C "$DIR" diff --cached --quiet || git -C "$DIR" commit -q -m "scalper save before update"
  git -C "$DIR" pull -q --rebase -X theirs --autostash origin main
else
  git clone -q "https://github.com/$REPO" "$DIR"
fi
cd "$DIR"

say "Running self-tests"
python3 tests.py | tail -1

say "Installing the scalper service"
sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<UNIT
[Unit]
Description=Paper scalper (crypto majors, 1-min signals, fake money)
After=network-online.target
Wants=network-online.target

[Service]
User=${USER:-$(id -un)}
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-/etc/paper-desk.env
ExecStart=/usr/bin/python3 -m jev_bot scalp --crypto majors --fx none --port $PORT --publish-every 15 --account state/scalper_account.json
Restart=always
RestartSec=15
MemoryMax=300M

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE" >/dev/null 2>&1
sleep 25
sudo systemctl --no-pager --lines=0 status "$SERVICE" | sed -n 1,3p
if curl -fsS "http://127.0.0.1:$PORT/api/state" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("prices for:", ", ".join(sorted(d["prices"])) or "none yet")'; then :; else
  echo "The dashboard isn't answering yet. Check:  journalctl -u $SERVICE -n 50"
fi

IP=$(curl -fsS https://checkip.amazonaws.com 2>/dev/null || echo "<your-server-ip>")
say "Done"
cat <<MSG
The scalper is running: crypto majors, live Binance prices, exits checked every second.

  Live dashboard:  http://$IP:$PORT     (updates every second)
                   First allow port $PORT in Lightsail: instance -> Networking ->
                   IPv4 Firewall -> Add rule -> Custom TCP, port $PORT -> Create.
  Snapshot:        https://$(echo "${REPO%%/*}" | tr 'A-Z' 'a-z').github.io/${REPO##*/}/scalper.html  (every ~15 min)
  Live log:        journalctl -u $SERVICE -f
  Stop scalper:    sudo systemctl disable --now $SERVICE
MSG
