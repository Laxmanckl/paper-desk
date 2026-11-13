#!/usr/bin/env bash
# Takes the latest code and settings from GitHub and restarts the Jev Desk.
# Paper accounts, open positions and logs are kept.
#   bash ~/paper-desk/server/update.sh
set -euo pipefail
cd "$HOME/paper-desk"
sudo systemctl stop jev-desk 2>/dev/null || true
git add state/jev_desk.json 2>/dev/null || true
git diff --cached --quiet 2>/dev/null || git commit -q -m "jev desk save before update" || true
git pull -q --rebase -X theirs --autostash origin main
echo "Code updated to: $(git log -1 --format='%h %s')"
.venv/bin/python -m pip install -q "pandas>=2.2" "numpy>=1.26" || echo "(could not check the Python packages; carrying on)"
sudo systemctl enable jev-desk >/dev/null 2>&1 || true
sudo systemctl start jev-desk
sleep 8
sudo systemctl --no-pager --lines=0 status jev-desk | sed -n 1,3p
echo "--- last lines of the desk's log ---"
sudo journalctl -u jev-desk --no-pager -n 12 -o cat || true
echo "--- can the server push to GitHub? ---"
git push -q --dry-run origin HEAD:main && echo "yes" || echo "NO: the dashboard will not update until this is fixed"
