# Jev Desk · gold + forex

A trading system for gold and the forex majors. Jev makes narrow judgments, plain code
makes the decisions, and a risk engine always has the final word. It's built stage by
stage from *How to Build a 24/7 AI Trading System With Jev*. The guide's design is a
crypto market maker; here it's adapted to directional trading on MT5.

**Paper only.** The bridge only reads prices, and there is no order code anywhere in this repo.

- **Dashboard:** https://laxmanckl.github.io/paper-desk/ (updates every ~15 min)
- **Live page:** `http://<your-lightsail-ip>:8080` (updates every second)
- **Instruments:** XAU/USD, EUR/USD, GBP/USD, AUD/USD, USD/JPY, USD/CAD, USD/CHF, NZD/USD

> The previous Paper Desk (daily bot + scalper) is saved on the
> [`archive/paper-desk`](https://github.com/Laxmanckl/paper-desk/tree/archive/paper-desk) branch.

## How it runs

```
Windows PC                     Lightsail server                         GitHub
MetaTrader 5 (demo, logged in)
  └─ MT5 bridge ──prices──▶  jev-desk service                 ──state──▶  Pages dashboard
     every 10 s, read-only     · decides every 15 min           every     · hourly "is the
                               · live page :8080                15 min      server alive?" check
                               · logs every snapshot
```

| Where | What it does |
|---|---|
| **Windows PC** | The `JevBridge` task reads quotes and closed 5-minute bars from MT5, converts the broker's clock to UTC, and sends them to the server every 10 s with a secret token. It finds your broker's symbol names (e.g. `XAUUSD.m`, `GOLD`) by itself. A watchdog restarts it if MT5 freezes. |
| **Lightsail** | The `jev-desk` service builds a snapshot per instrument every 15 min, a few seconds after a 5-minute bar closes. It serves the live page and pushes `state/jev_desk.json` to GitHub every 15 min. Every snapshot stays in `logs/` for 14 days, for the calibration check. |
| **GitHub** | Rebuilds the dashboard after each push. Emails you (failed run) if the server goes quiet. |

When the PC is off, MT5 is closed or it's the weekend, prices go stale. The desk then
holds and decides nothing; the dashboard says why.

## Setup

**1. Lightsail server** (browser SSH terminal):
```bash
curl -fsSL https://raw.githubusercontent.com/Laxmanckl/paper-desk/main/server/setup.sh | bash
```
At the end it prints the **server address** and **bridge token** for step 2. Port 8080 must
be open in Lightsail → Networking → IPv4 Firewall (it already is if the old scalper's live
page worked).

**2. Windows PC.** Open MetaTrader 5 and log in to your demo account. Then, in PowerShell (not as administrator):
```powershell
irm https://raw.githubusercontent.com/Laxmanckl/paper-desk/main/bridge/setup-bridge.ps1 -OutFile $env:TEMP\setup-bridge.ps1
powershell -ExecutionPolicy Bypass -File $env:TEMP\setup-bridge.ps1
```
Paste the address and token when asked. The setup does a test push, then starts the bridge
at every logon and keeps the PC from sleeping while plugged in.

## Everyday use

| You want to… | Do this |
|---|---|
| Watch it | Dashboard link, or the live page |
| Server log | `journalctl -u jev-desk -f` |
| Bridge log (PC) | `Get-Content C:\jev-bridge\bridge.log -Tail 20 -Wait` |
| Decide every 5 or 10 min | Change `--every 15` in `/etc/systemd/system/jev-desk.service`, then `sudo systemctl daemon-reload && sudo systemctl restart jev-desk` |
| Update the code | Run the setup command again (server, PC, or both) |
| Stop | Server: `bash ~/paper-desk/server/stop.sh`. PC: `Unregister-ScheduledTask JevBridge -Confirm:$false` |

## Build stages (guide Step 7, adapted)

| # | Stage | Status |
|---|---|---|
| 1 | State engine: snapshot per instrument, under 400 tokens, no future data | **done** |
| 2 | Jev decision layer: 6 questions per instrument in one call, `jev-1.13.0` pinned and logged | next |
| 3 | Policy engine: long / short / exit / stand aside by confidence thresholds | |
| 4 | Risk engine: hard limits, a stop on every trade, veto on every order | |
| 5 | Loop + fallback ladder: stale = hold, Jev down = code rules, limit = kill | |
| 6 | Paper trading + calibration log, two weeks before any real money | |

### Snapshot fields (Step 3, adapted)

Retail MT5 brokers expose no real order book, so the guide's depth, queue and imbalance
fields become quote-based ones:

| Group | Fields |
|---|---|
| Price | `mid`, returns over 15 min / 1 h / 4 h / 1 day (bps) |
| Cost | `spread_pips`, `spread_vs_norm` (vs the median spread over the last day) |
| Volatility | `atr_pips` (5-min ATR 14), `atr_vs_5d`, `day_range_pips`, `range_pos` (0 = day low, 1 = day high) |
| Trend & momentum | distance from EMA 20 / EMA 100 in ATRs, EMA 20 slope, `rsi14` |
| Activity & clock | `tickvol_ratio` (last hour vs normal hour), `session` (0 Asia, 1 London, 2 London+NY, 3 NY, 4 late), minutes into session |
| US dollar | USD index over 1 h and 1 day (average dollar move across the 7 majors) |
| Our book | position, unrealized pips, minutes held, drawdown |
| Data health | quote age, last bar age, bars in the last 24 h |

Every field is a number or empty, never a guess. A bar counts only after it closes, and a
test checks that a snapshot for time *t* is identical however much later data arrives.

## Code

```
jevsys/
  fx_state.py      Stage 1 state engine (snapshot per instrument)
  market.py        price inbox from the bridge; rejects unclosed bars and future timestamps
  instruments.py   symbols, pip sizes, USD direction
  desk.py          15-min decision loop + everything the dashboard shows; stages 2-5 plug into _decide()
  web.py           live page, /api/state, /api/ingest (token)
  publish.py       pushes state to GitHub
  sim_fx.py        price simulator for tests and `run --sim`
bridge/            mt5_bridge.py + Windows setup and watchdog
server/            Lightsail setup / stop
tests/             26 tests: python3 -m pytest -q
```

Try it anywhere without MT5: `python3 -m jevsys run --sim --every 5`, then open http://localhost:8080
