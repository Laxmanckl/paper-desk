#!/usr/bin/env bash
# Turns trading back on after the DRAWDOWN kill switch (the daily-loss one resets itself at 00:00 UTC).
# Only do this once you understand why the desk lost that much. Peak equity restarts from current equity.
set -euo pipefail
touch "$HOME/paper-desk/state/RESET_KILL"
echo "Reset requested. The desk picks it up within a few seconds; check the Activity log on the live page."
