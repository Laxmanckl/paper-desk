# The research loop

Both dashboards now carry a **Research loop** panel. It shows where each strategy stands in the cycle a quant desk runs, and the hard limits that guard it:

```
idea → written prediction → honest backtest → sealed test → paper → weekly critic → ledger → next idea
```

Paper only, as before. Nothing here can place a real order.

## What is where

| Piece | Where it lives | What it does |
| --- | --- | --- |
| Kill switch | `config/risk.json`, enforced in `jev_bot/killswitch.py` | Checked in code before every new trade on every desk. Daily loss, drawdown from peak, open positions, order size, a per-desk pause and a `kill_all`. Open trades keep their stops. |
| Hypothesis ledger | `state/ledger.json` | Every idea ever tested, its attempts, regime, results and lessons. Failed ideas are kept. |
| Written prediction | inside each ledger entry | Written before testing, then locked with a sha256 hash. It can never be edited. |
| Honest backtest | `jev_bot/research/honest.py` | Newest 25% of history sealed; walk-forward windows; 2x costs; delayed fills; worst stretches; Deflated Sharpe using every attempt counted. |
| Weekly critic | `jev_bot/research/review.py`, Sundays via `.github/workflows/research.yml` | Paper vs backtest vs prediction, what the week's losing trades had in common, repeated mistakes, keep / watch / pause. |

## From your phone

- **Pause a desk:** edit `config/risk.json` on GitHub (pencil icon), set that desk's `"enabled": false`. The servers pick it up within ~15 minutes.
- **Stop everything:** set `"kill_all": true`.
- **After a drawdown halt:** review, then either raise `max_drawdown_pct` or set `peak_reset` to any new text (for example today's date) to measure the drawdown from now.
- **Run the critic now / backtest the daily bot:** Actions → research → Run workflow → type `review`, `backtest H1`, or `sealed H1`.

## Commands

```bash
python -m jev_bot research list
python -m jev_bot research search --market XAUUSD --status failed
python -m jev_bot research add "Gold mean-reverts after 3 down days in a choppy regime" \
    --market XAUUSD --source "Price Analyst" --falsify "loses money over 40+ trades"
python -m jev_bot research predict H4 --return 6 --win-rate 45 --max-dd 8 --min-trades 30 --fail-return-below 0
python -m jev_bot research backtest H4            # development period only (add --source sim for a dry run)
python -m jev_bot research revise H4 "stop 1.2 ATR instead of 1.5"   # counts as an attempt; cap 5
python -m jev_bot research sealed H4              # exactly once per idea
python -m jev_bot research review                 # the critic
python -m jev_bot research export-mt5 EURUSD XAUUSD --days 60       # Windows PC: history for scalper tests
```

Scalper desks (H2, H3) are tested on 1-minute history from MT5. On the Windows PC:

```bash
python -m jev_bot research export-mt5 EURUSD GBPUSD XAUUSD --days 60
python -m jev_bot research backtest H2 --csv EURUSD=data/mt5/EURUSD_M1.csv --csv GBPUSD=data/mt5/GBPUSD_M1.csv --csv XAUUSD=data/mt5/XAUUSD_M1.csv
```

Then commit `state/ledger.json` (the scalper's next publish does it for you if you run it inside `C:\paper-scalper`).

## The three strategies already running

H1 (daily), H2 (scalper A) and H3 (scalper B) reached paper trading before this loop existed. They have no prediction written up front and earlier tuning was not counted. The honest thing to do now:

1. Write a **forward** prediction for each (`research predict H1 ...`). It is marked as covering paper trading from the day it was written.
2. Run the honest backtest (`backtest H1` on GitHub; H2 and H3 on the MT5 PC).
3. Let the Sunday critic compare paper results against both.

## Research agents (Claude Code prompts)

Run each agent separately so they do not anchor on each other. Each one checks the ledger first.

> You are the [Price Analyst | Macro Watcher | Skeptic]. Look only at [price and volume | rates, data releases, USD and gold drivers | why each idea fails] for [XAUUSD and EUR/USD]. Before proposing anything, run `python -m jev_bot research search --market XAUUSD` and `--status failed`, and do not repeat any failed idea unless the market regime has clearly changed. Propose at most 3 testable ideas. For each: the idea in one sentence, why it might work, the market conditions it needs, and what result would prove it wrong. Add each with `python -m jev_bot research add "..." --market ... --source "[ROLE]" --falsify "..."`. Do not read other agents' notes.

An idea becomes testable once it is wired into a desk's strategy code (`research wire H4 daily --strategy "jev_bot/meanrev.py v1"`); its prediction must be written before the first backtest.
