#!/usr/bin/env bash
# Stores your TypeSafe API key on this server and fires the guide's first test decision.
#
#   bash ~/paper-desk/server/jev-key.sh
#
# The key is saved only in /etc/jev-desk.env (readable by root only). It is never
# written to the repo, the logs or the dashboard. Run again any time to replace it.
set -euo pipefail
DIR="$HOME/paper-desk"; ENV=/etc/jev-desk.env; PY="$DIR/.venv/bin/python"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
[ -x "$PY" ] || { echo "Run the main setup first:  curl -fsSL https://raw.githubusercontent.com/Laxmanckl/paper-desk/main/server/setup.sh | bash"; exit 1; }

say "TypeSafe API key"
echo "Paste the key from https://console.typesafe.ai/keys. Nothing will show while you paste. Press Enter after."
read -rsp "Key: " RAW </dev/tty; echo
RAW=${RAW//$'\e[200~'/}; RAW=${RAW//$'\e[201~'/}          # strip browser paste markers
KEY=$(printf '%s' "$RAW" | tr -d '[:space:]')
[ -n "$KEY" ] || { echo "No key entered."; exit 1; }
printf '%s' "$KEY" | LC_ALL=C grep -q '^[[:graph:]]*$' || { echo "That key has unexpected characters. Copy it again."; exit 1; }

say "Firing the first decision (guide Step 2.4)"
cd "$DIR"
if ! TYPESAFE_API_KEY="$KEY" "$PY" -m jevsys jev-test; then
  echo
  echo "The key was NOT saved. Fix the problem above and run this again."
  exit 1
fi

say "Saving the key"
sudo touch "$ENV"
sudo sed -i '/^TYPESAFE_API_KEY=/d' "$ENV"
printf 'TYPESAFE_API_KEY=%s\n' "$KEY" | sudo tee -a "$ENV" >/dev/null
sudo chmod 600 "$ENV"
unset KEY RAW

say "Restarting the desk with Jev switched on"
sudo systemctl restart jev-desk
sleep 6
sudo systemctl --no-pager --lines=0 status jev-desk | sed -n 1,3p
curl -fsS http://127.0.0.1:8080/api/state | python3 -c 'import json,sys; j=json.load(sys.stdin)["jev"]; print("Jev:", j["status"], "| model", j["model"])' || true
echo
echo "Done. From the next decision round (every 15 min) Jev answers for each instrument with fresh prices."
echo "Watch it on the live page, or:  journalctl -u jev-desk -f"
