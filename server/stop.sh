#!/usr/bin/env bash
# Stops the server bot and hands trading back to GitHub's hourly runs.
set -euo pipefail
cd "$HOME/paper-desk"
sudo systemctl disable --now paper-desk
git add state/paper_account.json
git commit -q -m "Final server check" || true
git pull -q --rebase -X theirs origin main
echo github > state/runner.txt
git add state/runner.txt
git commit -q -m "Hand trading back to GitHub" || true
git push -q origin HEAD:main
echo "Stopped. GitHub runs the bot hourly again, starting from the same account."
