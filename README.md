# Jev Desk

A 24/7 trading system with Jev making fast judgments, plain code making the decisions,
and a risk engine that always has the final word. It's built stage by stage from
*How to Build a 24/7 AI Trading System With Jev*.

**Paper only.** No broker or wallet code exists in this repo, so nothing here can place a real order.

- **Dashboard:** https://laxmanckl.github.io/paper-desk/ (updates every ~15 min)
- **Live page:** `http://<your-lightsail-ip>:8080` (updates every second)

> The previous gold + EUR/USD Paper Desk is saved on the
> [`archive/paper-desk`](https://github.com/Laxmanckl/paper-desk/tree/archive/paper-desk) branch.

## How it runs

It uses the same setup the Paper Desk used:

| Where | What it does |
|---|---|
| **Lightsail server** | Runs the block loop around the clock as the `jev-desk` service. Serves the live page on port 8080. Pushes `state/jev_desk.json` to GitHub every 15 min. Keeps every snapshot in `logs/` (14 days) for the calibration check. |
| **GitHub Actions** | Rebuilds the dashboard on Pages after each push. Checks every hour that the server is alive; if it has gone quiet, the run fails and GitHub emails you. |

The loop itself can't run on GitHub. The guide needs a decision every 300 ms block, and
Actions runs start at most every 5 minutes and stop after 6 hours.

## Switching the server over (one time)

1. **Lightsail server.** Open the browser SSH terminal and run:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/Laxmanckl/paper-desk/main/server/setup.sh | bash
   ```
   This stops and removes the old `paper-desk` and `paper-scalper` services. It reuses the
   GitHub token already on the server, installs `jev-desk`, and prints both links.
2. **Windows PC (only if the MT5 scalper is still running there).** Run
   `server/windows/retire-scalper.ps1` in PowerShell, or paste its lines. Otherwise it keeps
   pushing the old scalper files to this repo.

## Everyday use

| You want to… | Do this |
|---|---|
| Watch it | Open the dashboard link, or the live page |
| See the server log | `journalctl -u jev-desk -f` |
| Update to the newest code | Run the setup command again |
| Stop it | `bash ~/paper-desk/server/stop.sh` |

## Build stages (guide Step 7)

| # | Stage | Status |
|---|---|---|
| 1 | State engine: numeric snapshot under 400 tokens, no future data | **done** |
| 2 | Jev decision layer: 6-question battery, direct API, `jev-1.13.0` pinned and logged | next |
| 3 | Policy engine: confidence thresholds in config | |
| 4 | Risk engine: hard limits, can veto any order | |
| 5 | Block loop + fallback ladder: late = hold, Jev down = code rules, limit = kill | |
| 6 | Paper mode + calibration log, two weeks before any real money | |

Until a venue is connected, the desk runs on a simulated order book with 300 ms blocks.
The dashboard says so in a banner.

## Code

```
jevsys/
  state_engine.py  Stage 1: snapshot per block (price, book, flow, vol, cross-market, our book, health)
  book_state.py    position, PnL, drawdown, holding time
  desk.py          block loop + everything the dashboard shows; stages 2-5 plug into _decide()
  feeds/           venue adapters (sim.py now; a real order book next)
  web.py           live page + /api/state
  publish.py       pushes state to GitHub
  dashboard*.html  the page (same look as the Paper Desk)
  snapshot_log.py  daily JSONL logs, gzipped after a day
tests/             23 tests: python3 -m pytest -q
```

Run it on any machine: `python3 -m jevsys run --port 8080` (add `--publish-every 15` to push to GitHub).
