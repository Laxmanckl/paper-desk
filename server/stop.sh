#!/usr/bin/env bash
# Stops the Jev Desk and pushes its final state. Run setup.sh again to restart.
set -euo pipefail
cd "$HOME/paper-desk"
sudo systemctl disable --now jev-desk
git add state/jev_desk.json
git commit -q -m "Jev desk final state" || true
git pull -q --rebase -X theirs origin main
git push -q origin HEAD:main
echo "Stopped. The dashboard keeps the last state; GitHub's hourly check will report the server as silent."
