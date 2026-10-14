#!/usr/bin/env bash
# Tests every rung of the fallback ladder on purpose (guide Step 6) and sends one Telegram test message.
# Runs on a simulated market in a temporary folder: the live paper account is never touched.
#   bash ~/paper-desk/server/drill.sh
set -euo pipefail
cd "$HOME/paper-desk"
sudo env $(sudo cat /etc/jev-desk.env 2>/dev/null | grep -E '^TELEGRAM_' | xargs) .venv/bin/python -m jevsys drill --send
