#!/usr/bin/env bash
# Turns on Telegram alerts for the Jev Desk (guide Step 6, rung 5: "send yourself an alert").
#   bash ~/paper-desk/server/telegram.sh
# You get: kill switch, Jev down/back, MT5 bridge down/back, paper trades opened/closed,
# and a daily summary after the New York close. (Turn kinds on/off in config/desk.json -> "alerts".)
#
# If your old Paper Desk bot already had Telegram set up on this server, it can reuse it.
# Otherwise: in Telegram talk to @BotFather, send /newbot, and copy the token it gives you.
set -euo pipefail
DIR="$HOME/paper-desk"; ENV=/etc/jev-desk.env; OLD=/etc/paper-desk.env; PY="$DIR/.venv/bin/python"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
[ -x "$PY" ] || { echo "Run the main setup first."; exit 1; }

TOKEN=""; CHAT=""
if sudo test -f "$OLD" && sudo grep -q '^TELEGRAM_BOT_TOKEN=' "$OLD" && sudo grep -q '^TELEGRAM_CHAT_ID=' "$OLD"; then
  say "Found the Telegram bot your old Paper Desk used"
  read -rp "Use the same bot and chat for the Jev Desk? [Y/n] " ANS </dev/tty
  if [ -z "$ANS" ] || [[ "$ANS" =~ ^[Yy] ]]; then
    TOKEN=$(sudo sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$OLD" | head -1)
    CHAT=$(sudo sed -n 's/^TELEGRAM_CHAT_ID=//p' "$OLD" | head -1)
  fi
fi

if [ -z "$TOKEN" ]; then
  say "Telegram bot token"
  echo "Paste the token from @BotFather (looks like 123456789:AA...). Nothing shows while you paste."
  read -rsp "Token: " RAW </dev/tty; echo
  RAW=${RAW//$'\e[200~'/}; RAW=${RAW//$'\e[201~'/}
  TOKEN=$(printf '%s' "$RAW" | tr -cd 'A-Za-z0-9:_-')
  printf '%s' "$TOKEN" | grep -Eq '^[0-9]{5,}:[A-Za-z0-9_-]{30,}$' || { echo "That doesn't look like a bot token. Copy it again from @BotFather."; exit 1; }
  NAME=$(curl -sS "https://api.telegram.org/bot$TOKEN/getMe" | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["result"]["username"])
except Exception: sys.exit(1)') || { echo "Telegram did not accept that token. In @BotFather: /mybots -> your bot -> API Token."; exit 1; }
  say "Link your Telegram chat"
  echo "Open Telegram, search for @$NAME, tap Start (or send it any message)."
  read -rp "Press Enter here after you've sent it a message... " _ </dev/tty
  for i in 1 2 3 4 5 6; do
    CHAT=$(curl -fsS "https://api.telegram.org/bot$TOKEN/getUpdates" | python3 -c '
import json,sys
r=json.load(sys.stdin).get("result",[])
ids=[u[k]["chat"]["id"] for u in r for k in ("message","my_chat_member") if k in u]
print(ids[-1] if ids else "")')
    [ -n "$CHAT" ] && break
    echo "Waiting for your message to @$NAME..."; sleep 5
  done
  [ -n "$CHAT" ] || { echo "No message found. Send @$NAME a message, then run this again."; exit 1; }
fi

say "Sending a test message"
cd "$DIR"
TELEGRAM_BOT_TOKEN="$TOKEN" TELEGRAM_CHAT_ID="$CHAT" "$PY" -m jevsys alert-test || { echo "Not saved. Fix the problem above and run again."; exit 1; }

say "Saving and restarting the desk"
sudo touch "$ENV"
sudo sed -i '/^TELEGRAM_BOT_TOKEN=/d;/^TELEGRAM_CHAT_ID=/d' "$ENV"
printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n' "$TOKEN" "$CHAT" | sudo tee -a "$ENV" >/dev/null
sudo chmod 600 "$ENV"
unset TOKEN RAW
sudo systemctl restart jev-desk
echo
echo "Done. Alerts are on. To turn them off: sudo sed -i '/^TELEGRAM_/d' $ENV && sudo systemctl restart jev-desk"
