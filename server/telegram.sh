#!/usr/bin/env bash
# Turns on Telegram alerts for the paper-trading bot on this server.
#   bash ~/paper-desk/server/telegram.sh
# Before running: in Telegram, talk to @BotFather, send /newbot, follow the
# prompts, and copy the token it gives you.
set -euo pipefail
DIR="$HOME/paper-desk"; ENV=/etc/paper-desk.env; UNIT=/etc/systemd/system/paper-desk.service
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "Telegram bot token"
echo "Paste the token from @BotFather (looks like 123456789:AA...). Nothing shows while you paste."
read -rsp "Token: " TOKEN </dev/tty; echo
[ -n "$TOKEN" ] || { echo "No token entered."; exit 1; }
NAME=$(curl -fsS "https://api.telegram.org/bot$TOKEN/getMe" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["username"])') \
  || { echo "That token did not work. Copy it again from @BotFather and re-run."; exit 1; }

say "Link your Telegram chat"
echo "Open Telegram, search for @$NAME, tap Start (or send it any message, like hi)."
read -rp "Press Enter here after you've sent it a message... " _ </dev/tty
CHAT=""
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

say "Saving settings"
printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n' "$TOKEN" "$CHAT" | sudo tee "$ENV" >/dev/null
sudo chmod 600 "$ENV"
if ! grep -q "EnvironmentFile" "$UNIT"; then
  sudo sed -i "s|^Environment=PYTHONUNBUFFERED=1|Environment=PYTHONUNBUFFERED=1\nEnvironmentFile=-$ENV|" "$UNIT"
  sudo systemctl daemon-reload
fi

say "Sending a test message"
cd "$DIR"
git pull -q --rebase --autostash origin main || true
TELEGRAM_BOT_TOKEN="$TOKEN" TELEGRAM_CHAT_ID="$CHAT" python3 -m jev_bot alert-test
sudo systemctl restart paper-desk
echo
echo "Done. Check Telegram for the test message. The bot has been restarted with alerts on."
echo "To turn alerts off:  sudo rm $ENV && sudo systemctl restart paper-desk"
