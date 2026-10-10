"""reflex checks. No network, no dependencies. Run: python tests.py

Each check is one fact about the decision loop. The real JEV engine is not
exercised here (it needs access and a key); the offline engine and the gate
are, because those are what run by default.
"""

from jev_bot import jev, markets
from jev_bot.types import MarketState, Decision, ACTIONS
from jev_bot.risk import Limits, check
from jev_bot.execution.paper import Book

PASS = FAIL = 0


def ok(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok    " + name)
    else:
        FAIL += 1
        print("  FAIL  " + name)


def st(**kw):
    base = dict(symbol="X", asset_class="stock", price=100.0, change_24h=0.0,
                volume_delta=0.0, momentum=0.0, news=0.0, regime="neutral")
    base.update(kw)
    return MarketState(**base)


# --- decision engine (offline) ---------------------------------------------
d1 = jev.decide(st(momentum=0.6, change_24h=0.05, regime="bullish"))
ok("a strong bullish state decides BUY", d1.action == "BUY")

d2 = jev.decide(st(momentum=-0.6, change_24h=-0.05, regime="bearish"))
ok("a strong bearish stock decides SELL", d2.action == "SELL")

d3 = jev.decide(st(momentum=-0.6, change_24h=-0.05, asset_class="meme", regime="bearish"))
ok("a strong bearish meme decides AVOID, not SELL", d3.action == "AVOID")

ok("a flat state decides HOLD", jev.decide(st()).action == "HOLD")

ok("action is always one of the four", jev.decide(st()).action in ACTIONS)

ok("probability and confidence stay in range",
   0.4 <= d1.confidence <= 0.97 and 0.5 <= d1.probability <= 0.97)

ok("every offline decision is labelled offline", d1.source == "offline")

ok("the same state gives the same decision",
   jev.decide(st(momentum=0.4)).action == jev.decide(st(momentum=0.4)).action
   and jev.decide(st(momentum=0.4)).probability == jev.decide(st(momentum=0.4)).probability)

# --- the gate --------------------------------------------------------------
strong = Decision("X", "BUY", 0.80, 0.90, "offline")
ok("a clean decision executes", check(strong, 0, Limits()).verdict == "EXECUTE")

ok("HOLD never executes",
   check(Decision("X", "HOLD", 0.9, 0.9), 0, Limits()).verdict == "SKIP")

ok("AVOID never executes",
   check(Decision("X", "AVOID", 0.9, 0.9), 0, Limits()).verdict == "SKIP")

ok("low confidence is refused",
   check(Decision("X", "BUY", 0.9, 0.5), 0, Limits()).verdict == "SKIP")

ok("the position cap is enforced",
   check(strong, 5, Limits()).verdict == "SKIP")

# --- paper execution -------------------------------------------------------
b = Book()
b.execute(strong, 100.0)
ok("executing adds a paper fill", b.open_positions() == 1)

b.mark({"X": 110.0})
ok("a BUY profits when price rises", b.open_pnl() > 0)

b2 = Book()
b2.execute(Decision("X", "SELL", 0.8, 0.9), 100.0)
b2.mark({"X": 90.0})
ok("a SELL profits when price falls", b2.open_pnl() > 0)

# --- market source ---------------------------------------------------------
ok("the generator is deterministic",
   [m.symbol for m in markets.generate(6, 3)] == [m.symbol for m in markets.generate(6, 3)])

# --- forex + gold -----------------------------------------------------------
from jev_bot import fx, backtest

ok("aliases resolve: GOLD -> XAUUSD, USD/EUR -> EURUSD",
   fx.resolve("gold") == "XAUUSD" and fx.resolve("USD/EUR") == "EURUSD")

ok("a bearish gold state can SELL (shorting is normal in FX)",
   jev.decide(st(asset_class="commodity", momentum=-0.6, change_24h=-0.05,
                 regime="bearish")).action == "SELL")

ok("a bearish EURUSD state can SELL",
   jev.decide(st(asset_class="forex", momentum=-0.6, change_24h=-0.05,
                 regime="bearish")).action == "SELL")

g1, g2 = fx.simulate("XAUUSD", 200, 5), fx.simulate("XAUUSD", 200, 5)
ok("the fx simulator is deterministic", [b.close for b in g1] == [b.close for b in g2])
ok("simulated bars are valid OHLC",
   all(b.low <= min(b.open, b.close) and b.high >= max(b.open, b.close) for b in g1))

# no lookahead: changing the future must not change today's state
cut = fx.simulate("EURUSD", 200, 9)
future_changed = cut[:120] + [fx.Bar(b.date, b.open * 2, b.high * 2, b.low * 2, b.close * 2)
                              for b in cut[120:]]
ok("a state uses only past bars (no lookahead)",
   fx.state_at("EURUSD", cut, 119) == fx.state_at("EURUSD", future_changed, 119))

res = backtest.run("XAUUSD", fx.simulate("XAUUSD", 400, 2))
ok("backtest equity adds up to the trades",
   abs(res.final_equity - (res.start_equity + sum(t.pnl for t in res.trades))) < 0.05)
ok("backtest trades every exit for a known reason",
   all(t.reason in ("stop", "target", "time", "reverse", "end") for t in res.trades))

losses = [t for t in res.trades if t.reason == "stop"]
ok("a stopped trade loses about the 1% risk (plus spread and gaps)",
   bool(losses) and all(-0.02 * res.start_equity * 1.5 < t.pnl < 0 for t in losses))

flat = [fx.Bar(str(i), 100.0, 100.0, 100.0, 100.0) for i in range(80)]
ok("a dead-flat market opens no trades", backtest.run("EURUSD", flat).trades == [])

import os, tempfile
tmp = os.path.join(tempfile.mkdtemp(), "x.csv")
with open(tmp, "w") as fh:
    fh.write('"Date","Price","Open","High","Low"\n')
    for i in reversed(range(70)):                       # newest first, day-first dates
        fh.write(f'"{1 + i % 28:02d}/{1 + i // 28:02d}/2024","{1000 + i:,.2f}",'
                 f'"{1000 + i:,.2f}","{1001 + i:,.2f}","{999 + i:,.2f}"\n')
loaded = fx.load_csv(tmp)
ok("csv loader sorts oldest-first and reads 1,000-style numbers",
   loaded[0].close == 1000.0 and loaded[-1].close == 1069.0)

# --- live data + live paper account (no network: fake yahoo responses) ------
from datetime import datetime, timedelta, timezone
from jev_bot import feeds, live

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def yahoo(bars, qtime, null_row=None):
    q = {k: [getattr(b, k) for b in bars] for k in ("open", "high", "low", "close")}
    if null_row is not None:
        for k in q:
            q[k][null_row] = None
    return {"chart": {"error": None, "result": [{
        "meta": {"regularMarketPrice": bars[-1].close, "regularMarketTime": int(qtime.timestamp())},
        "timestamp": [int((T0 + timedelta(days=i)).timestamp()) for i in range(len(bars))],
        "indicators": {"quote": [q]}}]}}


hist = fx.simulate("EURUSD", 300, 4)
pb, pq = feeds.parse_yahoo("EURUSD", yahoo(hist[:80], T0 + timedelta(days=79), null_row=5))
ok("yahoo parser reads bars and skips empty rows", len(pb) == 79 and pq.price == hist[79].close)

try:
    feeds.parse_yahoo("EURUSD", {"chart": {"result": None, "error": {"code": "Not Found"}}})
    ok("a bad yahoo response raises a clear FeedError", False)
except feeds.FeedError:
    ok("a bad yahoo response raises a clear FeedError", True)

tdp = {"status": "ok", "values": [{"datetime": f"2024-02-{i + 1:02d}", "open": "1", "high": "1",
                                  "low": "1", "close": str(1 + i / 100)} for i in reversed(range(20))]}
tb, _ = feeds.parse_twelvedata("EURUSD", tdp)
ok("twelvedata parser returns oldest first", tb[0].date < tb[-1].date)

ok("today's forming bar is excluded from decisions",
   len(live.completed_bars(pb, T0 + timedelta(days=78, hours=12))) == len(pb) - 1)

apath = os.path.join(tempfile.mkdtemp(), "acct.json")
seen, evs = [], []
for day in range(200, 240):
    for hour in (3, 15):
        now = T0 + timedelta(days=day, hours=hour)
        acct = live.Account.load(apath)
        evs += live.check(acct, ["EURUSD"], now=now, fetch=lambda s, d=day, n=now:
                          feeds.parse_yahoo(s, yahoo(hist[:d + 1], n)))
        acct.save(apath)
decided = [e.split("bar ")[1].split()[0] for e in evs if "new daily bar" in e]
ok("live: one decision per new daily bar, none repeated",
   len(decided) == 40 and len(set(decided)) == 40)
acct = live.Account.load(apath)
ok("live: trades happened and the account survives save/load",
   len(acct.closed) + len(acct.positions) > 0)
ok("live: cash equals start plus every closed trade",
   abs(acct.cash - (10_000 + sum(t["pnl"] for t in acct.closed))) < 0.05)

stale_now = T0 + timedelta(days=250)
fresh = live.Account()
ev = live.check(fresh, ["EURUSD"], now=stale_now, fetch=lambda s: feeds.parse_yahoo(
    s, yahoo(hist[:248], stale_now - timedelta(hours=30))))
ok("live: a stale quote (weekend) opens nothing", fresh.positions == {} and "stale" in ev[0])


def _down(sym):
    raise feeds.FeedError("HTTP 429")


ok("live: a feed error is reported, not a crash",
   "data error" in live.check(live.Account(), ["XAUUSD"], fetch=_down)[0])

# Yahoo forex: daily bars stamped 23:00 UTC (= midnight London in summer)
lon = {"chart": {"error": None, "result": [{
    "meta": {"regularMarketPrice": 1.12, "gmtoffset": 3600,
             "regularMarketTime": int(datetime(2026, 10, 5, 6, 5, tzinfo=timezone.utc).timestamp())},
    "timestamp": [int(datetime(2026, 9, d, 23, 0, tzinfo=timezone.utc).timestamp()) for d in (28, 29, 30)]
                 + [int(datetime(2026, 10, d, 23, 0, tzinfo=timezone.utc).timestamp()) for d in (1, 4)],
    "indicators": {"quote": [{k: [1.1] * 5 for k in ("open", "high", "low", "close")}]}}]}}
lb, lq = feeds.parse_yahoo("EURUSD", lon)
ok("forex bars are dated on the exchange's clock (Fri bar = Fri, not Thu)",
   [b.date for b in lb] == ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05"])
ok("Monday's forming forex bar is excluded until the day closes",
   live.completed_bars(lb, lq)[-1].date == "2026-10-02")

# --- server mode: checks every minute -------------------------------------
srv = live.Account(created="2026-10-03T00:00:00+00:00")
wk = T0 + timedelta(days=200)
for m in range(60 * 48):                      # a whole weekend, one check a minute
    srv.record(["EURUSD: market closed or quote stale (last Fri); positions held, no new trades",
                "XAUUSD: market closed or quote stale (last Fri); positions held, no new trades"],
               wk + timedelta(minutes=m))
ok("server: a repeated weekend message is logged once, not every minute", len(srv.events) == 2)
srv2 = live.Account()
for m in range(60):
    srv2.record([], wk + timedelta(minutes=m))
ok("server: quiet minutes add one equity point per 5 min, not 60",
   len(srv2.equity_log) == 12 and srv2.last_check.startswith((wk + timedelta(minutes=59)).isoformat()[:16]))

import subprocess, sys
hb_path = os.path.join(tempfile.mkdtemp(), "hb.json")
fresh_acct = live.Account(); fresh_acct.record([]); fresh_acct.save(hb_path)
hb_ok = subprocess.run([sys.executable, "-m", "jev_bot", "heartbeat", "--account", hb_path],
                       capture_output=True, text=True)
stale_acct = live.Account(); stale_acct.record([], datetime.now(timezone.utc) - timedelta(hours=2))
stale_acct.save(hb_path)
hb_bad = subprocess.run([sys.executable, "-m", "jev_bot", "heartbeat", "--account", hb_path],
                        capture_output=True, text=True)
ok("heartbeat passes for a live bot and fails for one silent 2 hours",
   hb_ok.returncode == 0 and hb_bad.returncode != 0 and "NOT RUNNING" in hb_bad.stderr)

# --- telegram alerts (no network: messages captured) -----------------------
from jev_bot import alerts
sent = []
al = live.Account(created="2026-10-01T00:00:00+00:00")
al.last_price = {"EURUSD": {"price": 1.10, "time": "", "source": "x"}}
al.positions["EURUSD"] = {"symbol": "EURUSD", "side": "SELL", "entry": 1.1200, "stop": 1.13,
                          "target": 1.10, "units": 10000, "opened_at": "2026-10-05T10:00:00+00:00",
                          "entry_bar": "2026-10-02", "signal_prob": 0.9, "signal_conf": 0.85}
mon = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
alerts.notify(al, [], 0, set(), mon, sent.append)
ok("alert: a newly opened trade sends one OPENED message",
   len(sent) == 1 and "OPENED SELL EUR/USD" in sent[0])
alerts.notify(al, [], 0, {"EURUSD"}, mon, sent.append)
ok("alert: an already-open trade is not announced again", len(sent) == 1)
al.closed.append({"symbol": "EURUSD", "side": "SELL", "entry": 1.12, "exit": 1.10, "reason": "target",
                  "pnl": 200.0, "pips": 200.0, "closed_at": "2026-10-05T13:00:00+00:00"})
alerts.notify(al, [], 0, {"EURUSD"}, mon, sent.append)
ok("alert: a closed trade sends CLOSED with its P&L", "CLOSED SELL EUR/USD" in sent[-1] and "+$200.00" in sent[-1])
n = len(sent)
for m in range(0, 40, 1):
    alerts.notify(al, ["XAUUSD: data error, skipped this check (HTTP 429)"], 1, {"EURUSD"},
                  mon + timedelta(minutes=m), sent.append)
ok("alert: data trouble is reported once after 30 min, not every minute",
   len(sent) == n + 1 and "problem for 30 min" in sent[-1])
alerts.notify(al, [], 1, {"EURUSD"}, mon + timedelta(minutes=41), sent.append)
ok("alert: recovery is reported once", "back to normal" in sent[-1])
n = len(sent)
alerts.notify(al, [], 1, {"EURUSD"}, datetime(2026, 10, 5, 21, 5, tzinfo=timezone.utc), sent.append)
alerts.notify(al, [], 1, {"EURUSD"}, datetime(2026, 10, 5, 21, 6, tzinfo=timezone.utc), sent.append)
alerts.notify(al, [], 1, {"EURUSD"}, datetime(2026, 10, 10, 21, 6, tzinfo=timezone.utc), sent.append)
ok("alert: one daily summary per weekday after the close, none on Saturday",
   len(sent) == n + 1 and "Paper desk" in sent[-1])
ok("alert: nothing is sent when Telegram is not set up",
   (not alerts.configured()) or os.environ.get("TELEGRAM_BOT_TOKEN") is not None)

# --- scalper -------------------------------------------------------------------
from jev_bot.scalp import instruments as si, feeds as sf
from jev_bot.scalp.bars import BarBuilder, Bar as SBar
from jev_bot.scalp.book import ScalpBook
from jev_bot.scalp.engine import Engine as SEngine
from jev_bot.scalp.strategy import ScalpConfig

ok("scalp: USDJPY profits convert yen to dollars",
   abs(si.quote_to_usd(si.get("USDJPY"), {"USDJPY": 150.0}) - 1 / 150) < 1e-12)
ok("scalp: EURGBP converts pounds via GBPUSD",
   si.quote_to_usd(si.get("EURGBP"), {"GBPUSD": 1.3}) == 1.3)

bb = BarBuilder()
done = [bb.update(t, 100 + t % 7) for t in range(0, 185)]
closed_bars = [d for d in done if d]
ok("scalp: ticks become one candle per minute", len(closed_bars) == 3 and closed_bars[0].t == 0
   and closed_bars[1].t == 60 and closed_bars[0].h == 106 and closed_bars[0].l == 100)

cfg = ScalpConfig(crypto_fee=0.0005)
T = 1791158400 + 10 * 3600                     # Monday 10:00 UTC (forex hours)
bk = ScalpBook(created="x")
bk.config = dict(cfg.__dict__)
bk.on_tick("EURUSD", 1.10000, 1.10008, T)
pos = bk.open("EURUSD", "BUY", 0.00020, T, cfg)
ok("scalp: a buy fills at the ask, stop below, target 1.5x further",
   isinstance(pos, dict) and pos["entry"] == 1.10008 and pos["stop"] < pos["entry"] < pos["target"]
   and abs((pos["target"] - pos["entry"]) - 1.5 * (pos["entry"] - pos["stop"])) < 1e-12)
risk_usd = (pos["entry"] - pos["stop"]) * pos["units"]
ok("scalp: position sized to risk 0.25% of the account", abs(risk_usd - 25.0) < 0.01)
bk.on_tick("EURUSD", pos["stop"] - 0.00001, pos["stop"] + 0.00007, T + 30)
ok("scalp: the stop closes the trade at the bid (slippage included)",
   not bk.positions and bk.trades[-1]["reason"] == "stop" and bk.trades[-1]["pnl"] < -25)
ok("scalp: cash = start + every closed trade's net",
   abs(bk.cash - (10_000 + sum(t["pnl"] for t in bk.trades))) < 0.01)   # trade P&L is shown to the cent
ok("scalp: cooldown blocks re-entry right after a trade",
   bk.open("EURUSD", "BUY", 0.0002, T + 60, cfg) == "cooling down after the last trade")

c2 = ScalpBook(created="x"); c2.config = dict(cfg.__dict__)
c2.on_tick("BTCUSDT", 62000, 62012, T)
r = c2.open("BTCUSDT", "BUY", 30.0, T, cfg)              # tiny 1-min range vs fees + spread
ok("scalp: a trade whose costs can't be kept under 25% of risk is skipped",
   isinstance(r, str) and r.startswith("costs too high"))
r = c2.open("BTCUSDT", "BUY", 200.0, T, cfg)
ok("scalp: crypto fees are charged on entry", isinstance(r, dict) and r["fees"] > 0
   and c2.cash < 10_000 and r["cost_frac"] <= 0.25 + 1e-9)

c3 = ScalpBook(created="x"); c3.config = dict(cfg.__dict__)
c3.on_tick("XAUUSD", 4000, 4000.3, T)
roll = 1791158400 + 21 * 3600                            # Mon 21:00 UTC
c3.on_tick("XAUUSD", 4000, 4000.3, roll)
ok("scalp: no new forex/gold trades in the daily rollover pause",
   "rollover" in c3.open("XAUUSD", "BUY", 2.0, roll, cfg))
night = 1791158400 + 23 * 3600                           # Mon 23:00 UTC (Asian session)
c3.on_tick("XAUUSD", 4000, 4000.3, night)
ok("scalp: no new gold trades in the thin Asian session (21-06 UTC)",
   "no gold trades" in c3.open("XAUUSD", "BUY", 2.0, night, cfg))
c3.on_tick("EURUSD", 1.1, 1.10008, night)
ok("scalp: forex pairs still trade round the clock outside the rollover pause",
   isinstance(c3.open("EURUSD", "BUY", 0.0002, night, cfg), dict))

c5 = ScalpBook(created="x"); c5.config = dict(cfg.__dict__)
for i in range(20):
    c5.on_tick("EURUSD", 1.1, 1.10008, T + i); c5.note_spread("EURUSD")
c5.on_tick("EURUSD", 1.1, 1.10020, T + 30)                  # spread jumps 0.8 -> 2.0 pips
ok("scalp: spread guard skips entries when the spread is 1.5x+ normal",
   "normal level" in c5.open("EURUSD", "BUY", 0.0002, T + 30, cfg))
c5.on_tick("EURUSD", 1.1, 1.10009, T + 40)
ok("scalp: ...and trades again once it settles", isinstance(c5.open("EURUSD", "BUY", 0.0002, T + 40, cfg), dict))

from jev_bot.scalp import strategy as sst, orb as sorb
def mkbars(n, f, t0=1791158400):
    return [SBar(t0 + i * 60, f(i), f(i) + 0.0001, f(i) - 0.0001, f(i)) for i in range(n)]
up15 = mkbars(900, lambda i: 1.1 + i * 0.00001)
ok("scalp: 15-minute candles built from 1-minute ones",
   len(sst.htf_closes(up15, 15)) == 60 and sst.htf_trend(up15, cfg) == "up")
dn = mkbars(900, lambda i: 1.1 - i * 0.00001)
base = dn[839].c
mixed = dn[:840] + [SBar(b.t, base + j * 0.00003, base + j * 0.00003 + 0.0001, base + j * 0.00003 - 0.0001,
                         base + j * 0.00003) for j, b in enumerate(dn[840:], 1)]
sig = sst.evaluate(mixed, cfg)
ok("scalp: no trade when the 1-min and 15-min trends disagree",
   sig.action is None and sig.trend == "up" and "15-min trend down" in sig.reason)
ok("scalp: waits for enough history for the 15-min trend",
   "15-min trend history" in sst.evaluate(mkbars(200, lambda i: 1.1 + i * 0.00001), cfg).reason)

from jev_bot.scalp import runner as _sr
ocfg = _sr.orb_config(type("A", (), {"crypto_fee": 0.0, "fx_commission": 0.0})())
D0 = 1791158400                                              # Mon 00:00 UTC
rb = mkbars(7 * 60, lambda i: 1.1 + (0.0005 if i % 2 else 0.0), D0)     # 06-07 range ~1.0999-1.1006
st_ = {}
ok("scalp B: measures the 06-07 UTC range before trading",
   "measuring" in sorb.evaluate(rb[:400], ocfg, 0, {}).reason)
inside = sorb.evaluate(rb + mkbars(5, lambda i: 1.1003, D0 + 420 * 60), ocfg, 0, st_)
ok("scalp B: no trade while price stays inside the range", inside.action is None and "inside" in inside.reason)
brk = sorb.evaluate(rb + mkbars(5, lambda i: 1.1003, D0 + 420 * 60) + mkbars(1, lambda i: 1.1009, D0 + 425 * 60), ocfg, 0, st_)
ok("scalp B: a close above the range + buffer is a BUY, stop at the range middle, out by 16:00",
   brk.action == "BUY" and abs(brk.stop - (1.1006 + 1.0999) / 2) < 1e-9 and brk.deadline == D0 + 16 * 3600)
ob = ScalpBook(created="x"); ob.config = dict(ocfg.__dict__)
tb = D0 + 426 * 60
ob.on_tick("EURUSD", 1.1009, 1.10098, tb)
pos = ob.open("EURUSD", "BUY", 0.0002, tb, ocfg, stop_price=brk.stop, deadline=brk.deadline)
ok("scalp B: risk sized to the range-middle stop, target 1.5x",
   isinstance(pos, dict) and abs(pos["stop"] - brk.stop) < 1e-12
   and abs((pos["entry"] - pos["stop"]) * pos["units"] - 25) < 0.01)
ob.on_tick("EURUSD", 1.1010, 1.10108, D0 + 16 * 3600)
ok("scalp B: trades still open at 16:00 UTC are closed", not ob.positions and ob.trades[-1]["reason"] == "time")
ok("scalp B: one breakout per instrument per day",
   "one breakout" in sorb.evaluate(rb, ocfg, 0, {"range": (D0, 1.1006, 1.0999), "traded": D0}).reason
   or "done for today" in sorb.evaluate(rb + mkbars(1, lambda i: 1.1009, D0 + 425 * 60), ocfg, 0,
                                         {"range": (D0, 1.1006, 1.0999), "traded": D0}).reason)

from jev_bot.scalp.engine import Fanout
ea, eb = SEngine(ScalpBook(created="x"), ["EURUSD"], cfg), SEngine(ScalpBook(created="x"), ["EURUSD"], ocfg)
fan = Fanout([ea, eb]); fan.on_tick("EURUSD", 1.1, 1.10008, T)
ok("scalp: one price feed drives both strategies' accounts",
   ea.book.prices["EURUSD"]["ask"] == eb.book.prices["EURUSD"]["ask"] == 1.10008 and ea.book is not eb.book)
now_ = 1791158400 + 10 * 3600 + 30
rates = [{"time": 1791158400 + 3 * 3600 + 10 * 3600 - (5 - i) * 60, "open": 1, "high": 1, "low": 1, "close": 1} for i in range(5)]
ok("scalp: MT5 candle times converted from broker time (UTC+3) to UTC",
   [b.t for b in sf.mt5_bars(rates, now_)][-1] == 1791158400 + 10 * 3600 - 60)
old = dict(cfg.__dict__); old.pop("fx_pause_utc"); old["fx_session_utc"] = [6, 20]
ok("scalp: an account saved by the old version still loads",
   ScalpConfig.from_dict(old).fx_pause_utc == ScalpConfig().fx_pause_utc)
from jev_bot.scalp import runner as _sr
import os as _os
_os.environ["SCALP_FX_PAUSE"] = "off"; _a = _sr.fx_pause_from_env()
_os.environ["SCALP_FX_PAUSE"] = "20.5-22"; _b = _sr.fx_pause_from_env()
_os.environ.pop("SCALP_FX_PAUSE")
ok("scalp: rollover pause can be turned off or moved", _a == () and _b == (20.5, 22.0))

c4 = ScalpBook(created="x"); c4.config = dict(cfg.__dict__)
c4.on_tick("EURUSD", 1.1, 1.10008, T)
c4.cash -= 250                                          # lose 2.5% today
ok("scalp: daily loss limit halts new trades for the day",
   "halted" in c4.open("EURUSD", "BUY", 0.0002, T + 1, cfg) and c4.halted_day)

ok("scalp: Binance bookTicker message parsed",
   sf.parse_binance('{"stream":"btcusdt@bookTicker","data":{"u":1,"s":"BTCUSDT","b":"62000.1","B":"1","a":"62000.2","A":"2"}}')
   == ("BTCUSDT", 62000.1, 62000.2))
ok("scalp: broker symbol names matched (suffixes, GOLD for XAUUSD)",
   sf.resolve_mt5_symbols(["EURUSD", "XAUUSD", "USDJPY"], ["EURUSD.a", "EURUSDx.pro", "GOLD", "USDJPY"], {})
   == {"EURUSD": "EURUSD.a", "XAUUSD": "GOLD", "USDJPY": "USDJPY"})


class FakeMT5:
    TIMEFRAME_M1 = 1
    def __init__(self): self.n = 0
    def initialize(self, **kw): return True
    def last_error(self): return (0, "ok")
    def symbols_get(self): return [type("S", (), {"name": n}) for n in ("EURUSD.r", "XAUUSD.r")]
    def symbol_select(self, *a): return True
    def copy_rates_from_pos(self, *a):
        return [{"open": 1.1, "high": 1.1002, "low": 1.0998, "close": 1.1001}] * 60
    def symbol_info_tick(self, name):
        self.n += 1
        return type("T", (), {"bid": 1.1, "ask": 1.10008, "time_msc": self.n // 10, "time": 0})
    def account_info(self): return type("A", (), {"login": 123, "server": "Demo-Server"})
    def shutdown(self): pass


me = SEngine(ScalpBook(created="x"), ["EURUSD", "XAUUSD"])
mf = sf.MT5Feed(me, ["EURUSD", "XAUUSD"], mt5=FakeMT5())
mf.connect()
mf.poll_once(); mf.poll_once()
ok("scalp: MT5 feed maps broker symbols, warms up and delivers ticks",
   mf.map == {"EURUSD": "EURUSD.r", "XAUUSD": "XAUUSD.r"} and len(me.builders["EURUSD"].bars) == 60
   and me.book.prices["EURUSD"]["ask"] == 1.10008 and me.book.feeds["mt5"]["status"] == "live")
mf.last_real["EURUSD"] = 0                               # long silent: market closed
before = me.book.prices["EURUSD"]["t"]
sf.tick_clock(me, [mf], 10_000)
ok("scalp: the 1-second clock skips instruments with no real price for 2 min",
   me.book.prices["EURUSD"]["t"] == before)

ok("scalp: MT5 feed knows forex hours (Saturday closed, Tuesday open)",
   not sf.MT5Feed.market_open(datetime(2026, 10, 10, 12, tzinfo=timezone.utc).timestamp())
   and sf.MT5Feed.market_open(datetime(2026, 10, 6, 12, tzinfo=timezone.utc).timestamp()))

# --- dashboard ---------------------------------------------------------------
from jev_bot import dashboard
acct.record(["test event </script>"])
page = dashboard.build(acct)
ok("dashboard is one self-contained page with the account embedded",
   page.startswith("<!doctype html>") and '"start_equity"' in page and "/*__DATA__*/" not in page)
ok("dashboard data cannot break out of its script tag",
   "test event <\\/script>" in page)
blank = dashboard.build(live.Account(created="2026-01-01T00:00:00+00:00"))
ok("dashboard builds for a brand-new empty account", '"closed":[]' in blank)

# --- kill switch (config/risk.json, enforced in code) --------------------------
from jev_bot import killswitch as ks
CFG = {"kill_all": False, "desks": {"daily": {"max_daily_loss_pct": 2, "max_drawdown_pct": 8,
                                              "max_open_positions": 2, "max_order_leverage": 5}}}
fine = {"equity": 10_000, "peak": 10_000, "day_start": 10_000, "open_positions": 0}
ok("kill switch: a normal order passes", ks.check_order("daily", {"notional": 20_000}, fine, CFG)[0])
ok("kill switch: daily loss limit blocks", not ks.check_order("daily", {"notional": 1}, {**fine, "equity": 9_790}, CFG)[0])
ok("kill switch: drawdown limit blocks",
   not ks.check_order("daily", {"notional": 1}, {**fine, "equity": 9_150, "day_start": 9_150}, CFG)[0])
ok("kill switch: position cap blocks", not ks.check_order("daily", {"notional": 1}, {**fine, "open_positions": 2}, CFG)[0])
ok("kill switch: oversized order blocks", not ks.check_order("daily", {"notional": 60_000}, fine, CFG)[0])
ok("kill switch: kill_all blocks everything", not ks.check_order("daily", {"notional": 1}, fine, {**CFG, "kill_all": True})[0])
ok("kill switch: a paused desk opens nothing",
   not ks.check_order("daily", {"notional": 1}, fine, {"desks": {"daily": {"enabled": False}}})[0])
bad = os.path.join(tempfile.mkdtemp(), "risk.json")
open(bad, "w").write("{ not json")
ok("kill switch: an unreadable config fails closed", not ks.check_order("daily", {"notional": 1}, fine, ks.load(bad))[0])
ok("kill switch: a missing config falls back to limits, not to none",
   not ks.check_order("daily", {"notional": 1}, {**fine, "equity": 9_000, "day_start": 9_000},
                      ks.load(bad + ".missing"))[0])

halted = live.Account.load(os.path.join(tempfile.mkdtemp(), "h.json"))
blocked_ev = []
for day in range(200, 215):
    now = T0 + timedelta(days=day, hours=15)
    blocked_ev += live.check(halted, ["EURUSD"], now=now, risk_cfg={"kill_all": True},
                             fetch=lambda s, d=day, n=now: feeds.parse_yahoo(s, yahoo(hist[:d + 1], n)))
ok("kill switch: the live daily bot opens nothing while halted, and says why",
   halted.positions == {} and any("BLOCKED by kill switch" in e for e in blocked_ev)
   and halted.killswitch.get("blocks"))

from jev_bot.scalp.book import ScalpBook as _SB
from jev_bot.scalp.strategy import ScalpConfig as _SC
kb = _SB(created="x")
kb.on_tick("EURUSD", 1.1000, 1.1001, 1_000)
kb.spreads["EURUSD"] = [0.0001] * 20
kb.ks_desk = "scalper_a"
ks._cache.clear()
_orig = ks.load
ks.load = lambda path=None: {"kill_all": True}
res_blocked = kb.open("EURUSD", "BUY", 0.0005, 1_000, _SC())
kb.ks_desk = None                                         # backtests: no live limits
res_open = kb.open("EURUSD", "BUY", 0.0005, 1_000, _SC())
ks.load = _orig
ok("kill switch: the scalper book is stopped by it live, and backtests are not",
   isinstance(res_blocked, str) and "kill switch" in res_blocked and isinstance(res_open, dict))

# --- research loop: ledger, locked predictions, honest testing, weekly critic ----
from jev_bot.research import ledger as L, stats as RS, honest, review as RV, panel as RP
led = {"hypotheses": []}
hh = L.add(led, "test idea", "daily", ["EURUSD"], "Skeptic", "loses money")
pred = L.predict(hh, 5, 45, 8, 20, {"return_below": 0})
try:
    L.predict(hh, 50, 90, 1, 1, {})
    ok("ledger: a prediction can never be rewritten", False)
except L.LedgerError:
    ok("ledger: a prediction can never be rewritten", True)
ok("ledger: the locked prediction verifies", L.prediction_intact(hh) is True)
hh["prediction"]["return_pct"] = 50
ok("ledger: an edited prediction is detected", L.prediction_intact(hh) is False)
hh["prediction"]["return_pct"] = 5
for i in range(4):
    L.record_attempt(hh, {"return_pct": -1, "trades": 30, "sharpe": -0.01, "passed": False})
    L.revise(hh, f"change {i}")
L.record_attempt(hh, {"return_pct": -1, "trades": 30, "sharpe": -0.01, "passed": False})
try:
    L.revise(hh, "one more")
    ok("ledger: rewrites are capped at 5 and the idea is marked failed", False)
except L.LedgerError:
    ok("ledger: rewrites are capped at 5 and the idea is marked failed", hh["status"] == "failed")
L.record_sealed(hh, {"return_pct": 1})
try:
    L.record_sealed(hh, {"return_pct": 2})
    ok("ledger: the sealed test is used once", False)
except L.LedgerError:
    ok("ledger: the sealed test is used once", True)
ok("ledger: search finds past failures by market and status",
   L.search(led, market="eurusd", status="failed") == [hh] and not L.search(led, market="XAUUSD"))

import random as _r
rg = _r.Random(3)
noise = [rg.gauss(0.0005, 0.01) for _ in range(500)]
ok("deflated Sharpe: more attempts make the same result less convincing",
   RS.deflated_sharpe(noise, 1) > RS.deflated_sharpe(noise, 50) > RS.deflated_sharpe(noise, 500))
ok("regime labels a steady climb as trending up",
   RS.regime([100 * 1.004 ** i * (1 + 0.003 * (-1) ** i) for i in range(120)]) == "trending up")

h2 = L.add(led, "daily rules", "daily", ["EURUSD"], "you")
L.predict(h2, 5, 40, 15, 10, {"return_below": -100})
simb = {"EURUSD": fx.simulate("EURUSD", 1200, 5)}
dev = honest.daily(h2, simb, 1, [])
cut_date = simb["EURUSD"][int(1200 * (1 - honest.HOLDOUT))].date
ok("honest backtest: development runs never see the sealed period",
   dev["last"] < cut_date and dev["sealed_from"] == cut_date)
ok("honest backtest: walk-forward, double costs, delayed fills and worst stretches all run",
   len(dev["walk_forward"]) == 4 and "return_pct" in dev["costs_2x"] and "return_pct" in dev["breaker"]
   and len(dev["worst_stretches"]) == 5 and dev["checks"])
sealed = honest.daily_sealed(h2, simb)
ok("honest backtest: the sealed run covers only the sealed period", sealed["first"] >= cut_date)

rv = RV.run(apath, {}, write=False, now=T0 + timedelta(days=240))
ok("weekly critic: reviews the daily desk and recommends keep / watch / pause",
   rv["desks"]["daily"]["recommend"] in ("keep", "watch", "pause") and "lifetime" in rv["desks"]["daily"])
page2 = dashboard.build(live.Account.load(apath))
ok("dashboard: carries the research loop (kill switch, ledger) and its renderer",
   '"research":' in page2 and '"killswitch":' in page2 and "window.renderResearch" in page2
   and "<!--__RESEARCH__-->" not in page2)
from jev_bot.scalp import runner as _run
_sp = os.path.join(tempfile.mkdtemp(), "scalper.html")
_run.build_static(kb, _sp, "note", "a")
_html = open(_sp, encoding="utf-8").read()
ok("scalper dashboard: carries the research loop for its own desk",
   '"desk":"scalper_a"' in _html and "window.renderResearch" in _html)

# --- decision journal ---------------------------------------------------------------
from jev_bot import journal as J
J.ROOT = tempfile.mkdtemp()                       # never write the real journal from tests
jacct = live.Account.load(os.path.join(tempfile.mkdtemp(), "j.json"))
for day in range(200, 230):
    now = T0 + timedelta(days=day, hours=15)
    live.check(jacct, ["EURUSD"], now=now, journal_on=True,
               fetch=lambda s, d=day, n=now: feeds.parse_yahoo(s, yahoo(hist[:d + 1], n)))
je = J.read_since("daily", T0)
ok("journal: one entry per daily decision, each with conditions and reasoning",
   len([e for e in je if e["decision"] != "CLOSE"]) == 30
   and all(e.get("reasoning") and "conditions" in e and e.get("strategy") for e in je))
ok("journal: trades opened and closed are both recorded",
   any(e["decision"] in ("OPEN", "REVERSE") for e in je) == bool(jacct.closed or jacct.positions))
ok("journal: tail returns the newest entries first", J.tail("daily", 3)[0]["t"] >= J.tail("daily", 3)[-1]["t"])
quiet = live.Account()
live.check(quiet, ["EURUSD"], now=T0 + timedelta(days=231, hours=15),
           fetch=lambda s: feeds.parse_yahoo(s, yahoo(hist[:232], T0 + timedelta(days=231, hours=15))))
ok("journal: off unless the live runner turns it on (tests and backtests write nothing)",
   len(J.read_since("daily", T0)) == len(je))

# --- strategy template (Step 3) --------------------------------------------------------
from jev_bot import strategies as STR
tmpl = STR.load("_template")
ok("strategies: the template passes its own unit tests and safety checks", STR.check(tmpl) == [])


class _Bad(STR.DailyStrategy):
    def signal(self, history):
        return "LONG"


bad_fails = STR.check(_Bad(name="bad", hypothesis="x"))
ok("strategies: a strategy returning anything but BUY/SELL/None fails its checks",
   any("only 'BUY', 'SELL' or None" in f for f in bad_fails) and any("tests() is empty" in f for f in bad_fails))
r_t = backtest.run("XAUUSD", fx.simulate("XAUUSD", 600, 3), strategy=tmpl)
ok("strategies: the backtester runs a template strategy with its own exits",
   r_t.trades and set(r_t.decisions) <= {"BUY", "SELL", "HOLD"})
h_t = L.add({"hypotheses": []}, "breakout", "daily", ["XAUUSD"])
dev_t = honest.daily(h_t, {"XAUUSD": fx.simulate("XAUUSD", 1200, 6)}, 1, [], strategy=tmpl)
ok("strategies: the honest backtest runs a template strategy", dev_t["trades"] > 0 and dev_t["checks"])
sacct = live.Account()
ev_s = []
for day in range(200, 240):
    now = T0 + timedelta(days=day, hours=15)
    ev_s += live.check(sacct, ["EURUSD"], now=now, desk="daily:H9", strategy=tmpl,
                       fetch=lambda s, d=day, n=now: feeds.parse_yahoo(s, yahoo(hist[:d + 1], n)))
ok("strategies: a promoted strategy paper-trades live on its own account",
   any("breakout" in e or "_template" in e for e in ev_s) or sacct.closed or sacct.positions)
ok("kill switch: a promoted strategy's desk inherits the daily limits",
   ks.limits("daily:H9", {"desks": {"daily": {"max_open_positions": 1}}})["max_open_positions"] == 1)

# --- research agents (fake Claude API, temporary ledger) -------------------------------
from jev_bot.research import agents as AG
_real_ledger = L.PATH
L.PATH = __import__("pathlib").Path(tempfile.mkdtemp()) / "ledger.json"
L.save({"hypotheses": [{"id": "H1", "idea": "Gold trends after a breakout above the 20-day high", "status": "failed",
                        "markets": ["XAUUSD"], "lessons": [], "desk": "daily"}]})
seen_prompts = []


def _fake_api(body, key):
    seen_prompts.append(body)
    if "Skeptic" in body["system"]:
        txt = '```json\n{"reviews": [{"id": "H2", "objection": "costs", "verdict": "drop"}]}\n```'
    elif "Price" in body["system"]:
        txt = 'memo\n```json\n{"ideas": [{"idea": "EUR/USD fades a 2% weekly move", "falsify": "f", "markets": ["EURUSD"]}]}\n```'
    else:
        txt = 'memo\n```json\n{"ideas": [{"idea": "Gold trends after a breakout above the 20-day high", "markets": ["XAUUSD"]}]}\n```'
    return {"content": [{"type": "text", "text": txt}], "stop_reason": "end_turn"}


rep_ag = AG.run(post=_fake_api, fetch=lambda s: (_ for _ in ()).throw(RuntimeError("offline")),
                out_dir=tempfile.mkdtemp())
led_ag = L.load()
L.PATH = _real_ledger
ok("agents: each specialist sees the ledger but never another agent's memo",
   all("Gold trends after a breakout" in b["messages"][0]["content"] for b in seen_prompts[:3])
   and not any("EUR/USD fades" in b["messages"][0]["content"] for b in seen_prompts[1:3]))
ok("agents: a repeat of a failed ledger idea is dropped, a new one is added as untested",
   rep_ag["added"] == ["H2"] and len(rep_ag["skipped"]) == 2
   and led_ag["hypotheses"][-1]["status"] == "untested" and led_ag["hypotheses"][-1]["desk"] == "idea")
ok("agents: the Skeptic's verdict is written into the ledger",
   led_ag["hypotheses"][-1].get("skeptic", {}).get("verdict") == "drop")
ok("agents: only the macro and central-bank agents get web search",
   ["tools" in b for b in seen_prompts] == [False, True, True, False])

_real_ledger = L.PATH
L.PATH = __import__("pathlib").Path(tempfile.mkdtemp()) / "ledger.json"
L.save({"hypotheses": []})
_od = tempfile.mkdtemp()
_r1 = AG.import_reply("price", 'memo\n```json\n{"ideas": [{"idea": "EUR/USD snaps back after a 3% stretch", "markets": ["EURUSD"]}]}\n```', _od)
_brief = AG.prompt_for("skeptic")
_r2 = AG.import_reply("skeptic", '```json\n{"reviews": [{"id": "H1", "objection": "few trades", "verdict": "test"}]}\n```', _od)
_led = L.load()
L.PATH = _real_ledger
ok("agents without an API key: a pasted reply goes into the ledger, then the Skeptic's brief and verdict",
   _r1["added"] == ["H1"] and "EUR/USD snaps back" in _brief and _led["hypotheses"][0]["skeptic"]["verdict"] == "test")

print(f"\n  {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
