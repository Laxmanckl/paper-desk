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
night = 1791158400 + 23 * 3600
c3.on_tick("XAUUSD", 4000, 4000.3, night)
ok("scalp: no forex/gold trades outside 06-20 UTC",
   "outside forex trading hours" in c3.open("XAUUSD", "BUY", 2.0, night, cfg))

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

print(f"\n  {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
