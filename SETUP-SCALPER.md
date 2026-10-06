# The scalper: per-second paper trading on crypto, forex and gold

A second bot that runs **next to** the daily bot, with its own fake **$10,000**.
It looks for quick trades on 1-minute candles and checks every open trade's
stop and target **every second**. Paper only: it never sends an order anywhere.

| | Daily bot | Scalper |
| --- | --- | --- |
| Decisions | once a day | every minute |
| Exit checks | every minute | every second |
| Typical trade | days to weeks | 1 to 30 minutes |
| Instruments | gold, EUR/USD | 10 crypto majors + 8 forex majors + gold |
| Prices | Yahoo Finance | Binance (crypto), your MT5 broker (forex/gold) |
| Dashboard | GitHub page, every ~15 min | **live page on the server, every second**, plus a GitHub snapshot |

## How it trades

On every closed 1-minute candle, for each instrument:

1. **Trend:** fast average (EMA 20) above slow (EMA 50) = only buys; below = only sells.
2. **Entry:** a short pullback against the trend that has just turned back
   (RSI 7 went below 30 and is back above 40 for buys; mirror for sells).
3. **Costs check:** the spread plus fees for the round trip must be **at most 25% of the
   amount at risk**. If the 1-minute moves are too small for that, the bot skips the trade
   and says "costs too high right now".
4. **Size:** risks **0.25%** of the account per trade.
5. **Exit:** stop = 1.2 × the 1-minute range (wider if costs need it), target = 1.5 × the stop,
   or close after **30 minutes**.

Safety limits: one trade per instrument, max 5 open, 5-minute pause after each trade,
max 20 trades per instrument per day, **stops opening trades after −2% in a day**,
forex/gold trade **24/5** (Sunday 22:00 to Friday 21:00 UTC), except no *new* trades
20:45–22:00 UTC, the daily rollover when brokers' spreads jump. To trade through it too,
add `$env:SCALP_FX_PAUSE = 'off'` to `C:\paper-scalper-settings.ps1`.

**Updates install themselves:** after each save the Windows scalper pulls from GitHub, and if
the code changed it restarts itself 15 seconds later with the new version.

**Honest expectation:** scalping is the hardest style to make money with. Every trade pays
the spread (and fees on crypto), so the strategy must win often enough to cover that.
In simulated tests it **lost** in every run (about −4% over 10 days when markets trended,
about −9% when they didn't): it captures some trend, but not enough to cover costs. Real
prices may behave differently. That is exactly what the paper test is for. Judge it on at
least a few hundred trades.

---

## Part A: crypto scalper on your Linux server (5 minutes, no extra cost)

1. In the Lightsail browser terminal of your existing server, run:

   ```
   curl -fsSL https://raw.githubusercontent.com/Laxmanckl/paper-desk/main/server/scalper-setup.sh | bash
   ```

2. Open the dashboard port: Lightsail → your instance → **Networking** →
   **IPv4 Firewall** → **Add rule** → Application **Custom**, Protocol **TCP**,
   Port **8080** → **Create**.
3. Open **http://YOUR-SERVER-IP:8080** (the setup prints the exact link).
   It updates every second. A snapshot is also published every ~15 minutes at
   `https://laxmanckl.github.io/paper-desk/scalper.html`.

The live page is plain `http` and anyone with the link can view it. It only shows fake trades.

---

## Part B: forex majors + gold from your MT5 broker (Windows server)

This is the current plan: forex majors + gold only (crypto is off). Part A is optional.

MetaTrader 5's Python connection only works on **Windows**, with the MT5 program running.

**Cost:** an AWS Lightsail Windows server with 2 GB costs about **$22/month** (Windows plans
have no free months, and 1 GB is too tight for Windows + MT5). Cheaper options: many MT5 brokers
offer a **free Windows VPS** to clients; ask yours. Or run it on your own Windows PC while
testing (it only works while the PC is on).

1. **Create the Windows server:** Lightsail → **Create instance** → **Microsoft Windows** →
   **Windows Server 2022** → the **$22 (2 GB)** plan → name `paper-scalper-win` → Create.
2. **Connect:** click the instance → **Connect using RDP** (opens in the browser).
3. **Install MT5:** in the server's browser, download your broker's **MetaTrader 5**, install it,
   and **log in to your DEMO account**. Then in MT5: **Tools → Options → Expert Advisors →
   tick "Allow algorithmic trading"** → OK.
4. **Install the scalper:** open **PowerShell** (right-click → *Run as administrator*) and paste:

   ```
   irm https://raw.githubusercontent.com/Laxmanckl/paper-desk/main/server/windows/setup-scalper.ps1 | iex
   ```

   It asks for your **GitHub token** (the same kind as before), optionally your MT5 login
   (skip it if MT5 is already logged in), and optionally the Telegram token and chat id.
5. **Open port 8080** for this server in Lightsail (same steps as Part A).
6. **Only if you installed the crypto scalper (Part A)**, stop it on the Linux server so only one scalper trades the account:

   ```
   sudo systemctl disable --now paper-scalper
   ```

7. **Leave the Windows server correctly:** close the Remote Desktop window. Do **not** click
   *Sign out*: MT5 needs the session to stay open. After a reboot, connect once with RDP and
   the scalper starts again by itself.

**If your broker names symbols differently** (e.g. `GOLD` or `EURUSD.r`), the bot matches them
automatically. If it picks the wrong one, edit `C:\paper-scalper-settings.ps1` and set
`$env:MT5_SYMBOLS = 'XAUUSD=GOLD,EURUSD=EURUSD.r'`, then restart the server.

---

## Telegram

The scalper trades often, so it does **not** message every trade by default. You get:
a **daily summary** (trades, wins, net, costs), an alert if the **−2% daily limit** is hit,
and an alert if a price feed is **down for 5+ minutes**. To get every trade too, add
`Environment=SCALP_TRADE_ALERTS=1` to the service (Linux) or `$env:SCALP_TRADE_ALERTS = '1'`
to the settings file (Windows).

## Everyday commands (Linux)

| You want to… | Run |
| --- | --- |
| Watch it live | `journalctl -u paper-scalper -f` |
| Restart | `sudo systemctl restart paper-scalper` |
| Update code | run the Part A command again |
| Stop it | `sudo systemctl disable --now paper-scalper` |
| Start over with $10,000 | stop it, delete `~/paper-scalper/state/scalper_account.json`, start it |

## Test the rules yourself

```
python3 -m jev_bot scalp-backtest                         # simulated BTC, EUR/USD, gold
python3 -m jev_bot scalp-backtest --sweep 10 --days 10    # 10 simulated runs
python3 -m jev_bot scalp-backtest BTCUSDT --csv BTCUSDT=btc_1m.csv   # your own 1-min data
```
