# Gold (XAUUSD) + EUR/USD testing mode

This copy of jev-bot adds **gold** and **EUR/USD** with **real live prices** and
**paper trading only**: fake money, no broker, no real orders.

## Run it online with a dashboard (no laptop)

See **SETUP-GITHUB.md**: GitHub runs the bot every hour for free and hosts a
dashboard you can open on your phone. For checks every minute on an always-on
server, see **SETUP-SERVER.md**.

## Setup on your own computer

Requires Python 3.10+ and no other packages.

```bash
cd jev-bot
python tests.py                      # 45 checks should pass
```

## 1. Live paper trading (real prices, fake money)

```bash
python -m jev_bot live                 # one check now: gold + EUR/USD
python -m jev_bot live --watch 15      # keep running, check every 15 minutes
python -m jev_bot account              # balance, open trades, trade history
python -m jev_bot account --reset      # start over with a fresh $10,000
python -m jev_bot dashboard            # build docs/index.html, open it in a browser
```

What happens on each check:

1. It downloads the **real** latest price and daily history from Yahoo Finance
   (free, no sign-up): EUR/USD = `EURUSD=X`, gold = `GC=F`.
2. It checks any open fake trade: has the real price hit the stop-loss or
   take-profit? Has it been held 20 trading days?
3. **Once per new trading day**, it decides BUY / SELL / HOLD for each
   instrument, using completed days only (same rules as the backtest), and
   opens a fake trade at the real current price plus spread.
4. It saves everything to `paper_account.json`.

You can close the window at any time. The next time you run `live`, it picks
up where it stopped, and it also catches stops/targets that were hit while it
was off, using the daily high/low.

**How long to test:** at least **2–3 months** (about 40–60 trading days). The bot
makes one decision per day, so a few days tells you almost nothing.

**Keeping it running:**
- Easiest: leave `python -m jev_bot live --watch 15` running in a terminal.
- Or run `python -m jev_bot live` once a day, any time. Daily is enough
  for this strategy. On Windows you can schedule it with Task Scheduler; on
  Mac/Linux, cron (`0 */4 * * 1-5  cd /path/to/jev-bot && python3 -m jev_bot live >> live.log`).

**Notes on the data:**
- Yahoo's gold is COMEX gold **futures** (`GC=F`). It moves with spot XAU/USD but
  is usually a few dollars higher. For true spot prices, use Twelve Data:
  get a free key at twelvedata.com, then
  `set TWELVEDATA_API_KEY=yourkey` (Windows) or `export TWELVEDATA_API_KEY=yourkey`
  (Mac/Linux), and add `--source twelvedata`.
- Yahoo is free but unofficial. If it fails, the bot prints "data error, skipped
  this check" and tries again next time. Nothing breaks.
- Forex is closed on weekends. The bot sees the stale price and makes no trades.

## 2. Backtest on real history

```bash
python -m jev_bot backtest --source yahoo               # last 5 years, both
python -m jev_bot backtest gold --source yahoo --range 10y
python -m jev_bot decisions --source yahoo              # today's real signals
```

Downloaded prices are saved in `data/`, e.g. `data/XAUUSD_yahoo.csv`.
You can also use your own CSV (Investing.com, MetaTrader export, etc.):
`python -m jev_bot backtest gold --csv gold=XAU_USD_history.csv`

## 3. Simulated tests (no internet)

```bash
python -m jev_bot backtest                         # fake price history
python -m jev_bot backtest --sweep 30              # 30 different fake histories
python -m jev_bot backtest --sweep 30 --trendless  # random markets, no trends
python -m jev_bot backtest eurusd --trades-csv trades.csv   # save every trade
```

Names accepted: `gold`, `xau`, `XAUUSD`, `eurusd`, `EUR/USD`, `USD/EUR`.

## How a trade works in the backtest

| Rule | Setting | Change with |
| --- | --- | --- |
| Risk per trade | 1% of the account | `--risk 0.005` |
| Stop-loss | 1.5 × ATR (recent average daily range) | `--sl-atr 2` |
| Take-profit | 3 × ATR | `--tp-atr 2` |
| Max holding time | 20 days | `--max-hold 10` |
| Costs | spread: gold $0.30, EUR/USD 1 pip | `INSTRUMENTS` in `jev_bot/fx.py` |
| Entry | next day's open after a signal (no peeking at the future) | — |

Pip sizes: EUR/USD 1 pip = 0.0001; gold 1 pip = $0.10.

## Reading the results

- **Final equity vs buy & hold**: did it beat just holding?
- **Max drawdown**: the worst drop from a peak. Can you live with it?
- **Profit factor**: total wins ÷ total losses. Below 1.0 loses money; aim for well above 1.2 on real data.
- **Win rate**: around 35–45% is normal here, since wins are about 2× the size of losses.

## What the tests showed

- On simulated markets **with trends**, it usually makes a modest profit, because
  the bot follows trends and the simulator contains trends.
- On **random markets with no trends** (`--trendless`), it roughly breaks even or
  loses after costs. It has **no special predictive power**; it is a simple
  trend-following rule.
- So the real question is whether gold / EUR/USD trended enough over your test
  period, which only a test on **real CSV data** can answer.

## Limits

- The default "offline" brain is a simple momentum formula, not AI. `--engine jev`
  uses TypeSafe's JEV model but needs an early-access key and makes one call per
  day per instrument.
- No news, interest-rate or economic-calendar data. These drive FX and gold heavily.
  There is a `news_score()` hook in `fx.py` to add later.
- Daily bars only. Real slippage, swap/overnight fees and weekend gaps are not modelled.
- In India, retail forex is legal only through SEBI-registered brokers on Indian
  exchanges (INR pairs plus a few crosses such as EUR/USD). Check the RBI Alert List
  before using any platform. Not financial advice.
