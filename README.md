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

## Volume breakout: the main paper account (since 23 Oct 2026)

The Jev account lost money in its first week of paper trading (spread ate its 5-minute
trades) and hit its 6% drawdown kill switch. The desk's main account is now the
`volume_breakout` strategy. It was validated in the
[Trading-Bot](https://github.com/Laxmanckl/Trading-Bot) repo on 16 years of Octa MT5
hourly data with real spreads:

- untouched TEST period: +0.13R per trade, profit factor 1.22
- random entries beat it 0.4% of the time
- 3 of 4 walk-forward windows positive

It failed the training-strength and sample-size criteria, so it is paper only.

**Rules (hourly bars).** The setup is the first hourly close outside the Asian range
(00:00-07:00 UTC) between 07:00 and 11:00. It trades only if all four hold:

1. **Volume:** the breakout bar's tick volume is at least 1.5x that hour's 20-day average.
2. **VWAP:** price is on the right side of the VWAP anchored at yesterday's open.
3. **Volatility:** ATR is at least its 20-day average.
4. **Trend:** the daily trend agrees.

**Exits.** The stop is the far side of the range, capped at 2 ATR. The target is 2R.
Everything closes at 20:00 UTC.

**Limits, the same as the validation:**
- 0.5% risk per trade
- 3 open positions
- one trade per instrument per day
- no entries ±30 min around listed news
- no new trades for the rest of the day after a 2% loss or 4 losses in a row
- its own drawdown kill switch at 10%

**Expect about one trade a month.** Judge it over months, not days.

**Where things live:**

| What | Where |
|---|---|
| Strategy code | `jevsys/vbo/lib/`: a verbatim, hash-checked copy of the tested modules. Change it in Trading-Bot, re-validate, then copy it again. |
| Engine rules | `jevsys/vbo/runner.py` |
| Settings | `config/desk.json` → `vbo` |
| Prices | MT5 bridge 1.2+, which sends hourly bars with tick volume. It loads ~8000 bars of history per instrument on first contact. |

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

**3. Switch on Jev (Stage 2)** on the Lightsail server:
```bash
bash ~/paper-desk/server/jev-key.sh
```
It asks for your TypeSafe API key (from https://console.typesafe.ai/keys) without showing it,
fires the guide's first test decision, and only if Jev answers saves the key to
`/etc/jev-desk.env` (readable by root only) and restarts the desk.

## Stage 2: the Jev battery

Every round, each instrument with fresh prices gets one `system_one` call (all eight in
parallel) with its snapshot as `state` and six atomic questions:

| Key | Type | Question |
|---|---|---|
| `regime` | Choice | trending / ranging / volatile / quiet |
| `direction` | Choice | up / down / neutral over the next hour |
| `move_continues` | Noul | the latest move continues rather than reverses |
| `costs_abnormal` | Noul | the spread is abnormally wide |
| `entry_quality` | Score | Do not enter / Marginal / Good / Excellent |
| `exit_urgency` | Score | No urgency / Mild / Tighten the stop / Close now |

Following the guide: the direct TypeSafe API, the model pinned to `jev-1.13.0` (a different
model in a response is flagged), and the snapshot, answers, model and latency logged together
for the calibration check. Stale instruments are never sent to Jev. Answers later than 20 s are
discarded. Errors (bad key, rate limit, network) are shown on the dashboard and the round carries
on. Jev only judges; it does not decide to trade. That is Stage 3's job.

The SDK is the official `typesafe-sdk`, pinned to 0.7.4 in the server's private Python
environment (`~/paper-desk/.venv`), so it can't change underneath the thresholds either.

## Stages 3-4: policy, risk, paper trading

Each round, for every instrument with fresh prices and a Jev answer:

**Policy** (`jevsys/policy.py`). Code turns the answers into an action. Entering needs a
higher bar than exiting, because a wrong entry costs more than leaving early.

| Flat | In a position |
|---|---|
| Stand aside if the regime is confidently volatile or quiet, the direction is neutral or under 65%, or Jev gives less than a 30% chance that entry quality is Good or better (since 15 Oct; Jev's "move continues" read proved backwards, so it is tracked but no longer gates) | Exit on "Close now", or if Jev points the other way at 60%+ |
| Otherwise enter long or short | Tighten the stop on "Tighten the stop"; otherwise hold |

**Risk engine** (`jevsys/risk.py`). It has the final word:
- **Every position** gets a stop of 2.5 × the 5-minute ATR and a target of 1.5 × that distance.
- **Size** is set so hitting the stop costs 0.5% of equity.
- **Vetoes before every entry:** max open positions (4), max positions betting the same way on the US dollar (2), spread over 1.5× normal, stale price, Jev failing 3 rounds in a row, slow Jev answer, Friday after 19:00 UTC, stop too wide for the risk budget.
- **Between rounds,** every 2 s on live quotes: stop, target, a 6-hour maximum hold, and flat by Friday 20:30 UTC.
- **Kill switches:**
  - down 2% on the day: close everything, no trades until 00:00 UTC;
  - 6% below peak: close everything, off until `bash ~/paper-desk/server/reset-kill.sh`.

**Paper book** (`jevsys/paper.py`). Fake money, real prices:
- buys fill at the live ask and sells at the bid, so the spread is paid;
- stops fill at the price seen when hit, so slippage is real;
- swaps are not modelled.

**Spread is checked by code, not Jev.** Whether the spread is above normal is arithmetic, and
the guide says never to spend a Jev call on arithmetic. The first day's log showed Jev calling
normal spreads abnormal. So the risk engine's 1.5×-normal limit decides, and Jev's
"costs abnormal" answer is only logged for calibration (`policy.use_jev_costs: false`).

**All thresholds and limits are in [`config/desk.json`](config/desk.json),** each with a note.
Edit it on GitHub; the server picks it up within ~15 minutes. A bad edit is rejected and the
old values stay. Every decision and trade records the settings version it used.

## Stage 5: the fallback ladder (guide Step 6)

| Rung | When | What the desk does |
|---|---|---|
| 1 Normal | Jev healthy and confident | Trades on Jev + policy + risk |
| 2 Cautious | Jev healthy but unsure | Stands aside |
| 3 Late | Prices stale, or Jev answers too slowly | Holds; open positions stay on their stops |
| 4 Fallback | Jev unreachable 2 rounds in a row, or no key | **Code-only rules** (`jevsys/rules.py`) trade at half size; alert sent |
| 5 Kill | Daily loss or drawdown limit hit | Closes everything, stops trading, **alert sent** |

The code-only rules are plain trend-following: enter with the 20- and 100-bar EMAs when RSI
isn't stretched, exit when price crosses back over the 20 EMA. A **max leverage** check (10×)
sizes positions down so total open size stays within 10× equity.

**Phone alerts (Telegram):** kill switch, Jev down/back, MT5 bridge silent for 5+ minutes
while the market is open, each paper trade, and a daily summary after the New York close.
Set up with `bash ~/paper-desk/server/telegram.sh` (it can reuse your old Paper Desk bot).

**Drill:** `bash ~/paper-desk/server/drill.sh` breaks a simulated copy of the desk on purpose:
- Jev late;
- Jev unreachable;
- prices stopped;
- loss limit hit.

It checks each rung reacts as the guide says, then sends one test alert. The live account is
never touched.

## Step 8: built in, so two weeks of paper trading answer it

Two **shadow paper accounts** trade the same live data, with the same costs and limits:
- **code-only rules**: the guide's "plain hand-written rules" baseline;
- **Jev without the confidence gate**: acts on Jev's top answer at any confidence.

Every hour the desk rebuilds the Step 8 report, shown on the dashboard and printed with
`cd ~/paper-desk && .venv/bin/python -m jevsys report`:

1. **Beats simpler versions?** Live account vs both shadows (needs 20+ trades each).
2. **Calibrated on your market?** Jev's direction calls against the price one hour later,
   bucketed by confidence (needs 100+ calls; passes at 10% average gap or less).
3. **Fees covered?** Average trade before costs vs spread and commission paid.
4. **Two weeks** of paper trading.

The report says **ready for a small real-money test** only when all of them pass.

## Everyday use

| You want to… | Do this |
|---|---|
| Watch it | Dashboard link, or the live page |
| Server log | `journalctl -u jev-desk -f` |
| Test Jev by hand | `cd ~/paper-desk && sudo env $(sudo cat /etc/jev-desk.env) .venv/bin/python -m jevsys jev-test` |
| Replace the key | `bash ~/paper-desk/server/jev-key.sh` |
| Change thresholds / limits | Edit `config/desk.json` on GitHub |
| Phone alerts | `bash ~/paper-desk/server/telegram.sh` |
| Test every ladder rung | `bash ~/paper-desk/server/drill.sh` |
| Step 8 report | `cd ~/paper-desk && .venv/bin/python -m jevsys report` |
| Restart the two-week test (after changing how it decides) | `bash ~/paper-desk/server/restart-test.sh` |
| Turn trading back on after the drawdown kill switch | `bash ~/paper-desk/server/reset-kill.sh` |
| Start the paper account over | `sudo systemctl stop jev-desk && rm ~/paper-desk/state/paper_book.json && sudo systemctl start jev-desk` |
| Bridge log (PC) | `Get-Content C:\jev-bridge\bridge.log -Tail 20 -Wait` |
| Decide every 5 or 10 min | Change `--every 15` in `/etc/systemd/system/jev-desk.service`, then `sudo systemctl daemon-reload && sudo systemctl restart jev-desk` |
| Update the code | Run the setup command again (server, PC, or both) |
| Stop | Server: `bash ~/paper-desk/server/stop.sh`. PC: `Unregister-ScheduledTask JevBridge -Confirm:$false` |

## Build stages (guide Step 7, adapted)

| # | Stage | Status |
|---|---|---|
| 1 | State engine: snapshot per instrument, under 400 tokens, no future data | **done** |
| 2 | Jev decision layer: 6 questions per instrument in one call, `jev-1.13.0` pinned and logged | **done** |
| 3 | Policy engine: long / short / exit / tighten / stand aside by confidence thresholds | **done** |
| 4 | Risk engine: sizing, a stop on every trade, vetoes, kill switches, paper fills | **done** |
| 5 | Fallback ladder: code-only rules when Jev is down, phone alerts, leverage limit, drill | **done** |
| 6 | Paper trading for two weeks + the Step 8 checks (shadow accounts and report built in) | **running** |

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
  jev.py           Stage 2: the 6-question battery, parallel calls, pinned model, error handling
  policy.py        Stage 3: Jev's answers -> enter / exit / tighten / hold / stand aside
  risk.py          Stage 4: sizing, stops, vetoes, exits, kill switches
  paper.py         paper book: fills at bid/ask, P&L, equity curve, saved on every change
  settings.py      loads and validates config/desk.json, hot reload
  rules.py         Stage 5: code-only fallback rules; the no-gate policy for the Step 8 shadow
  trader.py        one paper account + its trading code (live account and both shadows)
  alerts.py        Telegram alerts on a background thread
  drill.py         breaks a simulated desk on purpose to test every ladder rung
  report.py        Step 8: baselines, calibration, fees
  desk.py          15-min decision loop + everything the dashboard shows; stages 2-5 plug into _decide()
  web.py           live page, /api/state, /api/ingest (token)
  publish.py       pushes state to GitHub
  sim_fx.py        price simulator for tests and `run --sim`
bridge/            mt5_bridge.py + Windows setup and watchdog
server/            Lightsail setup / stop
tests/             93 tests: python3 -m pytest -q (Jev tests use the real SDK against a simulated API)
```

Try it anywhere without MT5: `python3 -m jevsys run --sim --every 5`, then open http://localhost:8080
