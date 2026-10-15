#!/usr/bin/env bash
# Starts the two-week paper test over: all three paper accounts back to $10,000, day 0.
# Logs are kept. Use after a change to how the desk decides, so the test runs on one setup.
#   bash ~/paper-desk/server/restart-test.sh
set -euo pipefail
cd "$HOME/paper-desk"
read -rp "Reset all paper accounts to \$10,000 and restart the two-week test? [y/N] " ANS </dev/tty
[[ "$ANS" =~ ^[Yy] ]] || { echo "Nothing changed."; exit 0; }
sudo systemctl stop jev-desk
# take the latest code and settings from GitHub first, so the new test runs on them
git add state/jev_desk.json 2>/dev/null || true
git diff --cached --quiet 2>/dev/null || git commit -q -m "jev desk save before update" || true
git pull -q --rebase -X theirs --autostash origin main && echo "Code updated to $(git log -1 --format='%h %s')"
.venv/bin/python -m jevsys restart-test --state state/jev_desk.json
sudo systemctl start jev-desk
sleep 5
sudo systemctl --no-pager --lines=0 status jev-desk | sed -n 1,3p
echo "Done. Day 0 of 14 starts now."
