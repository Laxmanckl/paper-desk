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
