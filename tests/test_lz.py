"""Lorentzian ML account: the ported model, the runner's rules and the desk wiring."""
import calendar
import json
import os
import random

import pytest

from jevsys.lz.lorentzian import Lorentzian, rational_quadratic
from jevsys.lz.runner import MIN_H1_BARS, LorentzianAccount, LzRisk, atr
from jevsys.market import MarketStore
from jevsys.paper import PaperBook
from jevsys.settings import Settings
from jevsys.trader import Trader
from jevsys.types import Bar, Quote

H = 3600
T0 = calendar.timegm((2026, 6, 1, 0, 0, 0))        # a Monday, 00:00 UTC


def walk(n, seed=4, start=1.10, vol=0.0012):
    """n hourly bars of a random walk, back to back from T0 (no weekends: only the order matters)."""
    rnd, p, bars = random.Random(seed), start, []
    for i in range(n):
        o = p
        p *= 1 + rnd.gauss(0, vol)
        hi = max(o, p) * (1 + abs(rnd.gauss(0, vol / 3)))
        lo = min(o, p) * (1 - abs(rnd.gauss(0, vol / 3)))
        bars.append(Bar(T0 + i * H, o, hi, lo, p, 1000.0, 10))
    return bars


def outputs(bars, **kw):
    m = Lorentzian(**kw)
    return [m.update(b.open, b.high, b.low, b.close) for b in bars]


KEYS = ("prediction", "signal", "start_long", "start_short")


# ---- the model -----------------------------------------------------------------------

def test_fast_search_matches_the_line_for_line_loop():
    bars = walk(2600)
    fast, slow = outputs(bars), outputs(bars, slow=True)
    assert [[o[k] for k in KEYS] for o in fast] == [[o[k] for k in KEYS] for o in slow]
    assert sum(o["start_long"] for o in fast) > 5 and sum(o["start_short"] for o in fast) > 5


def test_model_never_uses_the_future():
    bars = walk(1500)
    whole = outputs(bars)
    part = outputs(bars[:1200])
    assert [[o[k] for k in KEYS] for o in whole[:1200]] == [[o[k] for k in KEYS] for o in part]


def test_score_is_between_minus_8_and_8_and_only_the_first_2000_bars_are_kept():
    m = Lorentzian()
    for b in walk(2300):
        o = m.update(b.open, b.high, b.low, b.close)
        assert -8 <= o["prediction"] <= 8
    assert len(m.y) == 2000 and len(m.X) == 2000


def test_a_signal_needs_both_filters_and_the_kernel():
    for o in outputs(walk(2600)):
        if o["start_long"] or o["start_short"]:
            assert o["signal"] == (1 if o["start_long"] else -1)
    assert rational_quadratic(list(range(30))) > rational_quadratic(list(range(29)))   # rising prices: rising line


def test_warm_up_without_search_keeps_the_score_at_zero():
    m = Lorentzian()
    out = [m.update(b.open, b.high, b.low, b.close, predict=False) for b in walk(600)]
    assert all(o["prediction"] == 0 and not o["start_long"] and not o["start_short"] for o in out)


# ---- the runner ----------------------------------------------------------------------

@pytest.fixture
def setup(tmp_path):
    market = MarketStore()
    settings = Settings(os.path.join(os.path.dirname(__file__), "..", "config", "desk.json"))
    events, alerts = [], []
    tr = Trader("lz", "Lorentzian ML", PaperBook(str(tmp_path / "lz.json")), LzRisk(settings), live=True,
                event=events.append, alert=lambda t, k=None, c=0: alerts.append(t), prefix="[LZ] ")
    news = tmp_path / "news.csv"
    news.write_text("datetime,currency,event\n2026-01-01 12:00,USD,NFP\n")
    acct = LorentzianAccount(market, tr, settings, news_file=str(news))
    return market, tr, acct, events, alerts


def first_signal(bars, after=MIN_H1_BARS, side=None, weekday_ok=True):
    """Index of the first bar after `after` on which the model starts a trade (on a
    weekday hour when entries are allowed)."""
    import time
    for i, o in enumerate(outputs(bars)):
        want = o["start_long"] if side == "long" else o["start_short"] if side == "short" else (o["start_long"] or o["start_short"])
        g = time.gmtime(bars[i].ts + H)
        if i >= after and want and (not weekday_ok or (g.tm_wday < 4 and g.tm_hour < 22)):
            return i, ("long" if o["start_long"] else "short")
    raise AssertionError("no signal in the test data")


def load(market, sym, bars, quote_t, price, spread=0.0001):
    sd = market.data[sym]
    for b in bars:
        sd.h1.add(b)
    sd.quotes.append(Quote(sym, quote_t, price - spread / 2, price + spread / 2))


def test_a_model_signal_opens_a_position_at_the_next_open(setup):
    market, tr, acct, events, _ = setup
    bars = walk(1600)
    i, side = first_signal(bars)
    now = bars[i].ts + H + 5                              # 5 s after the signal bar closed
    load(market, "EURUSD", bars[:i + 1], now - 1, bars[i].close)
    acct.tick(now, {"EURUSD": market.data["EURUSD"].quotes[-1]})
    pos = tr.book.positions["EURUSD"]
    a = atr(bars[:i + 1])
    assert pos.side == side and acct.latest["EURUSD"]["status"] == "entered"
    assert abs(abs(pos.entry - pos.stop) - 2.0 * a) < 2e-5 and abs(abs(pos.target - pos.entry) - 4.0 * a) < 2e-5
    assert 0.4 <= pos.risk_usd / 10_000 * 100 <= 0.5      # sized to risk 0.5% at the stop
    assert any(f"OPEN EURUSD {side}" in e for e in events)


def test_bars_are_fed_once_and_a_quiet_bar_stands_aside(setup):
    market, tr, acct, *_ = setup
    bars = walk(1300)
    quiet = next(i for i, o in enumerate(outputs(bars)) if i >= MIN_H1_BARS and not (o["start_long"] or o["start_short"]))
    now = bars[quiet].ts + H + 5
    load(market, "EURUSD", bars[:quiet + 1], now - 1, bars[quiet].close)
    q = {"EURUSD": market.data["EURUSD"].quotes[-1]}
    acct.tick(now, q)
    n, fed = acct.evaluations, acct.models["EURUSD"]["model"].n
    acct.tick(now + 2, q)
    assert acct.evaluations == n == 1 and acct.models["EURUSD"]["model"].n == fed == quiet + 1
    assert acct.latest["EURUSD"]["status"] == "stand aside" and not tr.book.positions


def test_feeding_bar_by_bar_equals_loading_the_history(setup):
    market, tr, acct, *_ = setup
    bars = walk(1250)
    sd = market.data["EURUSD"]
    for b in bars[:1200]:
        sd.h1.add(b)
    acct.settings.data["lz"]["enabled"] = False            # only the model is under test here
    acct.tick(bars[1199].ts + H + 5, {})
    for k in range(1200, 1250):
        sd.h1.add(bars[k])
        acct.tick(bars[k].ts + H + 5, {})
    want = outputs(bars)[-1]
    assert acct.models["EURUSD"]["out"]["prediction"] == want["prediction"]
    assert acct.models["EURUSD"]["model"].n == 1250


def test_older_history_arriving_rebuilds_the_model(setup):
    market, tr, acct, *_ = setup
    bars = walk(1400)
    sd = market.data["EURUSD"]
    acct.settings.data["lz"]["enabled"] = False
    for b in bars[300:]:
        sd.h1.add(b)
    acct.tick(bars[-1].ts + H + 5, {})
    assert acct.models["EURUSD"]["model"].n == 1100
    for b in bars[:300]:                                   # the bridge delivers the older history later
        sd.h1.add(b)
    acct.tick(bars[-1].ts + H + 7, {})
    assert acct.models["EURUSD"]["model"].n == 1400


def test_short_history_waits_and_an_old_bar_is_not_traded(setup):
    market, tr, acct, *_ = setup
    bars = walk(1600)
    load(market, "EURUSD", bars[:500], bars[499].ts + H + 4, bars[499].close)
    acct.tick(bars[499].ts + H + 5, {"EURUSD": market.data["EURUSD"].quotes[-1]})
    assert acct.latest["EURUSD"]["status"] == "waiting"
    i, _ = first_signal(bars)
    late = bars[i].ts + H + 3 * H                          # found three hours after the bar closed
    load(market, "GBPUSD", bars[:i + 1], late - 1, bars[i].close)
    acct.tick(late, {"GBPUSD": market.data["GBPUSD"].quotes[-1]})
    assert acct.latest["GBPUSD"]["status"] == "idle" and not tr.book.positions


def test_switched_off_or_stale_price_does_not_trade(setup):
    market, tr, acct, events, _ = setup
    bars = walk(1600)
    i, side = first_signal(bars)
    now = bars[i].ts + H + 5
    load(market, "EURUSD", bars[:i + 1], now - 600, bars[i].close)        # the quote is 10 minutes old
    acct.tick(now, {"EURUSD": market.data["EURUSD"].quotes[-1]})
    assert acct.latest["EURUSD"]["status"] == "skipped" and "price data too old" in acct.latest["EURUSD"]["why"]
    acct.settings.data["lz"]["enabled"] = False
    load(market, "GBPUSD", bars[:i + 1], now - 1, bars[i].close)
    acct.tick(now, {"GBPUSD": market.data["GBPUSD"].quotes[-1]})
    assert acct.latest["GBPUSD"]["status"] == "off" and not tr.book.positions


def test_position_closes_after_four_bars(setup):
    market, tr, acct, *_ = setup
    bars = walk(1600)
    i, side = first_signal(bars)
    now = bars[i].ts + H + 5
    load(market, "EURUSD", bars[:i + 1], now - 1, bars[i].close)
    q = market.data["EURUSD"].quotes[-1]
    acct.tick(now, {"EURUSD": q})
    pos = tr.book.positions["EURUSD"]
    close_at = bars[i].ts + H + 4 * H
    assert tr.risk.exit_reason(pos, q, close_at - 10) is None
    assert "4 bars held" in tr.risk.exit_reason(pos, q, close_at + 1)
    hit = Quote("EURUSD", now + 60, pos.stop - 0.001 * pos.dir - 0.00005, pos.stop - 0.001 * pos.dir + 0.00005)
    assert tr.risk.exit_reason(pos, hit, now + 60) == "stop"


def test_an_opposite_signal_closes_and_reverses(setup):
    market, tr, acct, events, _ = setup
    bars = walk(2400)
    outs = outputs(bars)
    i, side = first_signal(bars)
    other = "short" if side == "long" else "long"
    j = next(k for k in range(i + 1, len(bars)) if outs[k]["start_" + other])
    sd = market.data["EURUSD"]
    for b in bars[:i + 1]:
        sd.h1.add(b)
    now = bars[i].ts + H + 5
    acct.tick(now, {"EURUSD": Quote("EURUSD", now - 1, bars[i].close - 0.00005, bars[i].close + 0.00005)})
    assert tr.book.positions["EURUSD"].side == side
    for b in bars[i + 1:j + 1]:                            # the position is held (the 4-bar exit is the desk's job)
        sd.h1.add(b)
    import time
    now2 = bars[j].ts + H + 5
    g = time.gmtime(now2)
    acct.tick(now2, {"EURUSD": Quote("EURUSD", now2 - 1, bars[j].close - 0.00005, bars[j].close + 0.00005)})
    assert any(f"model flipped to {other}" in e for e in events)
    assert len(tr.book.trades) == 1
    if g.tm_wday < 4:
        assert tr.book.positions["EURUSD"].side == other


def test_limits_max_open_loss_streak_and_kill_switch(setup):
    market, tr, acct, *_ = setup
    bars = walk(1600)
    i, side = first_signal(bars)
    now = bars[i].ts + H + 5
    q = Quote("EURUSD", now - 1, bars[i].close - 0.00005, bars[i].close + 0.00005)
    assert acct.vetoes("EURUSD", side, bars[:i + 1], now, q) == []
    acct.settings.data["lz"]["max_open_positions"] = 0
    assert any("max open positions" in v for v in acct.vetoes("EURUSD", side, bars[:i + 1], now, q))
    acct.settings.data["lz"]["max_open_positions"] = 3
    tr.book.trades += [{"sym": "GBPUSD", "opened": now - 900, "closed": now - 60 * k, "pnl": -10.0} for k in range(4, 0, -1)]
    assert any("4 losses in a row" in v for v in acct.vetoes("EURUSD", side, bars[:i + 1], now, q))
    wide = Quote("EURUSD", now - 1, bars[i].close - 0.01, bars[i].close + 0.01)
    assert any("twice the spread" in v for v in acct.vetoes("EURUSD", side, bars[:i + 1], now, wide))
    assert tr.risk.kill_check({"drawdown_pct": 7.0, "daily_loss_pct": 0.5}) is None
    assert tr.risk.kill_check({"drawdown_pct": 10.5, "daily_loss_pct": 0.5})[0] == "drawdown"
    assert tr.risk.kill_check({"drawdown_pct": 1.0, "daily_loss_pct": 2.1})[0] == "daily"


def test_settings_section_is_validated():
    from jevsys.settings import validate
    assert validate({})["lz"]["hold_bars"] == 4 and validate({})["lz"]["enabled"] is True
    with pytest.raises(ValueError):
        validate({"lz": {"hold_bars": 0}})
    with pytest.raises(ValueError):
        validate({"lz": {"enabled": "yes"}})
    with pytest.raises(ValueError):
        validate({"lz": {"made_up": 1}})


# ---- the desk --------------------------------------------------------------------------

def test_desk_runs_the_account_and_shows_it(tmp_path):
    from jevsys.dashboard import build as render
    from jevsys.desk import Desk
    from jevsys.jev import JevLayer
    market = MarketStore()
    d = Desk(market, state_path=str(tmp_path / "s.json"), log_dir=str(tmp_path / "logs"), jev=JevLayer())
    assert d.lz_trader in d.traders and d.lz_trader.book.path.endswith("lz_book.json")
    bars = walk(1600)
    i, side = first_signal(bars)
    now = bars[i].ts + H + 5
    sd = market.data["EURUSD"]
    for b in bars[:i + 1]:
        sd.h1.add(b)
    sd.quotes.append(Quote("EURUSD", now - 1, bars[i].close - 0.00005, bars[i].close + 0.00005))
    d.monitor_tick(now)
    st = d.state()
    assert st["lz"]["instruments"]["EURUSD"]["status"] == "entered"
    assert len(st["lz"]["book"]["positions"]) == 1 and st["lz"]["enabled"] is True
    assert any(f"[Lorentzian ML] OPEN EURUSD {side}" in e[1] for e in st["log"])
    page = render(json.loads(json.dumps(st, default=str)))
    assert "Lorentzian ML" in page and "lz-card" in page
    # four bars later the desk's monitor closes it
    end = bars[i].ts + H + 4 * H + 1
    sd.quotes.append(Quote("EURUSD", end - 1, bars[i].close - 0.00005, bars[i].close + 0.00005))
    d.monitor_tick(end)
    assert not d.lz_trader.book.positions and "4 bars held" in d.lz_trader.book.trades[-1]["why"]
