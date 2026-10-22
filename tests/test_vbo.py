"""Volume breakout account: the copied strategy code, the runner's engine rules,
and the desk wiring. Prices are built by hand so each expectation can be traced."""
import calendar
import hashlib
import json
import os
import time

import pytest

from jevsys.market import MarketStore
from jevsys.paper import PaperBook
from jevsys.settings import Settings
from jevsys.trader import Trader
from jevsys.types import Bar, Quote
from jevsys.vbo.runner import MIN_H1_BARS, VboRisk, VolumeBreakout

LIB = os.path.join(os.path.dirname(__file__), "..", "jevsys", "vbo", "lib")
HASHES = {  # Trading-Bot @ 0943a3c
    "decisions.py": "5325952af5685bf8f4e26e55985063b0ca2101a00b88dfecad90662520a05098",
    "indicators.py": "4b1a96dee37c52b058072c88bead66afd49b27d21c78c57a70411f9334115292",
    "intraday.py": "ff241527b3def16188dbde4b0cecf8511a33ec5282556b0242f4baac3534b2ba",
    "plans.py": "2666f34c215839817466cade8b259ee0c2d86fd47aae1cb99413e33041e43605",
    "strategy_vwap.py": "8c011e53e8ed840a9f52f70987b9667edd0f859794412162727be3acb7d360bc",
    "strategy_volume_breakout.py": "829fbb1ff07b7188f7c6eef51638731b371a66785b62392321e04ae2e32d6043",
    "news.py": "31003c381ece4d865c46c9fef04efc1ecde1fbc05e0f228b64074459f9655d8d",
}

DAY = calendar.timegm((2026, 10, 21, 0, 0, 0))   # a Wednesday, 00:00 UTC
H = 3600


def test_strategy_code_is_the_validated_copy():
    for name, digest in HASHES.items():
        with open(os.path.join(LIB, name), "rb") as fh:
            assert hashlib.sha256(fh.read()).hexdigest() == digest, f"{name} differs from the validated copy"


def history(breakout_volume=3000.0, n_days=100):
    """n_days of quiet hourly bars (10-pip range) in a slow uptrend, then a test
    day: seven Asian bars (00:00-06:00) stepping up through a 60-pip range, and
    a 07:00 bar that closes 3 pips above it. Returns (bars, breakout close, range low)."""
    bars, start = [], DAY - n_days * 86_400
    price = 1.0950
    for k in range(n_days * 24):
        ts = start + k * H
        price += 0.0000015
        bars.append(Bar(ts, price, price + 0.0005, price - 0.0005, price + 0.00001, 1000.0, 10))
    lo = price - 0.0005
    for h in range(7):            # 12-pip bars, each 8 pips higher: range lo .. lo + 60 pips
        b_lo = lo + h * 0.0008
        bars.append(Bar(DAY + h * H, b_lo + 0.0002, b_lo + 0.0012, b_lo, b_lo + 0.0010, 1000.0, 10))
    hi = lo + 6 * 0.0008 + 0.0012
    o, close = hi - 0.0004, round(hi + 0.0003, 5)
    bars.append(Bar(DAY + 7 * H, o, close + 0.0002, o - 0.0002, close, breakout_volume, 10))
    return bars, close, lo


@pytest.fixture
def setup(tmp_path):
    market = MarketStore()
    settings = Settings(os.path.join(os.path.dirname(__file__), "..", "config", "desk.json"))
    events, alerts = [], []
    tr = Trader("vbo", "Volume breakout", PaperBook(str(tmp_path / "vbo.json")), VboRisk(settings), live=True,
                event=events.append, alert=lambda t, k=None, c=0: alerts.append(t), prefix="[VB] ")
    news = tmp_path / "news.csv"
    news.write_text("datetime,currency,event\n2026-01-01 12:00,USD,NFP\n")
    vb = VolumeBreakout(market, tr, settings, news_file=str(news))
    return market, tr, vb, events, alerts, news


def load(market, sym, bars, quote_t, bid, ask):
    sd = market.data[sym]
    for b in bars:
        sd.h1.add(b)
    sd.quotes.append(Quote(sym, quote_t, bid, ask))


def test_breakout_on_volume_opens_a_position_at_the_next_open(setup):
    market, tr, vb, events, alerts, _ = setup
    bars, close, lo = history()
    assert len(bars) >= MIN_H1_BARS
    now = DAY + 8 * H + 20                          # 20 s after the 07:00 bar closed
    load(market, "EURUSD", bars, now - 1, close - 0.00005, close + 0.00005)
    vb.tick(now, {"EURUSD": market.data["EURUSD"].quotes[-1]})
    rec = vb.latest["EURUSD"]
    assert rec["status"] == "entered", rec
    assert rec["rvol"] == pytest.approx(3.0)
    pos = tr.book.positions["EURUSD"]
    assert pos.side == "long" and pos.entry == pytest.approx(close + 0.00005)   # filled at the ask
    # stop: far side of the range, capped at 2 x ATR
    assert pos.entry - pos.stop <= close - lo + 1e-9
    assert pos.target - pos.entry == pytest.approx(2.0 * (pos.entry - pos.stop), abs=2e-5)
    assert pos.risk_usd == pytest.approx(50, rel=0.05)             # 0.5% of $10,000
    assert any(e.startswith("[VB] OPEN EURUSD long") for e in events)
    assert alerts and alerts[0].startswith("[VB] 📈 Paper OPEN")


def test_same_bar_is_evaluated_once(setup):
    market, tr, vb, *_ = setup
    bars, close, _ = history()
    now = DAY + 8 * H + 20
    load(market, "EURUSD", bars, now - 1, close - 0.00005, close + 0.00005)
    vb.tick(now, {"EURUSD": market.data["EURUSD"].quotes[-1]})
    n = vb.evaluations
    vb.tick(now + 2, {"EURUSD": market.data["EURUSD"].quotes[-1]})
    assert vb.evaluations == n


def test_low_volume_breakout_stands_aside(setup):
    market, tr, vb, *_ = setup
    bars, close, _ = history(breakout_volume=1200.0)
    now = DAY + 8 * H + 20
    load(market, "EURUSD", bars, now - 1, close - 0.00005, close + 0.00005)
    vb.tick(now, {"EURUSD": market.data["EURUSD"].quotes[-1]})
    rec = vb.latest["EURUSD"]
    assert rec["status"] == "stand aside" and "low volume" in rec["why"]
    assert not tr.book.positions


def test_short_history_waits(setup):
    market, tr, vb, *_ = setup
    bars, close, _ = history(n_days=30)
    now = DAY + 8 * H + 20
    load(market, "EURUSD", bars, now - 1, close - 0.00005, close + 0.00005)
    vb.tick(now, {})
    assert vb.latest["EURUSD"]["status"] == "waiting"
    assert "loading hourly history" in vb.latest["EURUSD"]["why"]


def test_old_bar_is_not_traded(setup):
    market, tr, vb, *_ = setup
    bars, close, _ = history()
    now = DAY + 8 * H + 30 * 60                     # bar closed 30 min ago (bridge was down)
    load(market, "EURUSD", bars, now - 1, close - 0.00005, close + 0.00005)
    vb.tick(now, {"EURUSD": market.data["EURUSD"].quotes[-1]})
    assert vb.latest["EURUSD"]["status"] == "idle" and not tr.book.positions


def test_news_blackout_skips_the_entry(setup, tmp_path):
    market, tr, vb, events, _, news = setup
    news.write_text("datetime,currency,event\n2026-10-21 08:15,EUR,ECB\n")
    from jevsys.vbo.lib.news import load_news_filter
    vb.news = load_news_filter(str(news), 30)
    bars, close, _ = history()
    now = DAY + 8 * H + 20
    load(market, "EURUSD", bars, now - 1, close - 0.00005, close + 0.00005)
    vb.tick(now, {"EURUSD": market.data["EURUSD"].quotes[-1]})
    assert vb.latest["EURUSD"]["status"] == "skipped" and "news blackout" in vb.latest["EURUSD"]["why"]
    assert not tr.book.positions and any("skipped" in e for e in events)


def test_stale_quote_skips_the_entry(setup):
    market, tr, vb, *_ = setup
    bars, close, _ = history()
    now = DAY + 8 * H + 20
    load(market, "EURUSD", bars, now - 300, close - 0.00005, close + 0.00005)
    vb.tick(now, {})
    assert vb.latest["EURUSD"]["status"] == "skipped" and "too old" in vb.latest["EURUSD"]["why"]


def test_one_trade_per_instrument_per_day_and_max_open(setup):
    market, tr, vb, *_ = setup
    q = Quote("EURUSD", DAY + 8 * H, 1.1049, 1.1050)
    tr.book.trades.append({"sym": "EURUSD", "opened": DAY + 3 * H, "closed": DAY + 4 * H, "pnl": -5.0})
    plan = type("P", (), {"sl_dist": 0.002, "tp_dist": 0.004, "note": "x", "direction": 1})()
    assert "already traded this instrument today" in vb.vetoes("EURUSD", "long", plan, DAY + 8 * H + 20, q)
    for s in ("GBPUSD", "AUDUSD", "NZDUSD"):
        tr.book.open(s, "long", 0.1, Quote(s, DAY, 1.2, 1.2001), 1.19, 1.22, DAY, "test")
    assert "max open positions (3) reached" in vb.vetoes("USDJPY", "short", plan, DAY + 8 * H + 20,
                                                          Quote("USDJPY", DAY + 8 * H, 150.0, 150.01))


def test_four_losses_in_a_row_pause_the_day(setup):
    market, tr, vb, *_ = setup
    for k in range(4):
        tr.book.trades.append({"sym": "X", "opened": DAY + k * H, "closed": DAY + k * H + 60, "pnl": -10.0})
    plan = type("P", (), {"sl_dist": 0.002, "tp_dist": 0.004, "note": "x", "direction": 1})()
    v = vb.vetoes("EURUSD", "long", plan, DAY + 8 * H + 20, Quote("EURUSD", DAY + 8 * H, 1.1, 1.1001))
    assert any("4 losses in a row" in x for x in v)


def test_entries_only_in_the_london_session(setup):
    market, tr, vb, *_ = setup
    plan = type("P", (), {"sl_dist": 0.002, "tp_dist": 0.004, "note": "x", "direction": 1})()
    v = vb.vetoes("EURUSD", "long", plan, DAY + 13 * H, Quote("EURUSD", DAY + 13 * H, 1.1, 1.1001))
    assert any("outside 07:00-12:00" in x for x in v)


def test_exits_stop_target_and_2000_close(setup):
    market, tr, vb, *_ = setup
    risk = tr.risk
    pos = tr.book.open("EURUSD", "long", 0.3, Quote("EURUSD", DAY + 8 * H, 1.1049, 1.1050),
                       1.1030, 1.1090, DAY + 8 * H, "test")
    assert risk.exit_reason(pos, Quote("EURUSD", 0, 1.1060, 1.1061), DAY + 12 * H) is None
    assert risk.exit_reason(pos, Quote("EURUSD", 0, 1.1029, 1.1030), DAY + 12 * H) == "stop"
    assert risk.exit_reason(pos, Quote("EURUSD", 0, 1.1091, 1.1092), DAY + 12 * H) == "target"
    assert risk.exit_reason(pos, Quote("EURUSD", 0, 1.1060, 1.1061), DAY + 20 * H) == "20:00 UTC close"
    assert risk.exit_reason(pos, Quote("EURUSD", 0, 1.1060, 1.1061), DAY + 25 * H) == "20:00 UTC close"
    # no 6-hour limit (the Jev account's rule): a 10-hour-old position before 20:00 stays open
    assert risk.exit_reason(pos, Quote("EURUSD", 0, 1.1060, 1.1061), DAY + 18 * H) is None


def test_own_kill_switch_limits(setup):
    _, tr, *_ = setup
    assert tr.risk.kill_check({"drawdown_pct": 7.0, "daily_loss_pct": 0.5}) is None   # Jev's 6% is not used
    assert tr.risk.kill_check({"drawdown_pct": 10.5, "daily_loss_pct": 0.5})[0] == "drawdown"
    assert tr.risk.kill_check({"drawdown_pct": 1.0, "daily_loss_pct": 2.1})[0] == "daily"


def test_desk_runs_the_account_and_shows_it(tmp_path):
    from jevsys.desk import Desk
    from jevsys.jev import JevLayer
    market = MarketStore()
    d = Desk(market, state_path=str(tmp_path / "s.json"), log_dir=str(tmp_path / "logs"),
             jev=JevLayer())
    assert d.vbo_trader in d.traders
    bars, close, _ = history()
    now = DAY + 8 * H + 20                          # a fixed clock: 20 s after the 07:00 bar closed
    sd = market.data["EURUSD"]
    for b in bars:
        sd.h1.add(b)
    sd.quotes.append(Quote("EURUSD", now - 1, close - 0.00005, close + 0.00005))
    d.monitor_tick(now)
    st = d.state()
    assert st["vbo"]["instruments"]["EURUSD"]["status"] == "entered"
    assert len(st["vbo"]["book"]["positions"]) == 1
    assert any("[Volume breakout] OPEN EURUSD long" in e[1] for e in st["log"])
    assert "vbo" in json.loads(json.dumps(st, default=str))


def test_status_refreshes_when_history_arrives_for_the_same_bar(setup):
    market, tr, vb, *_ = setup
    bars, close, _ = history()
    now = DAY + 8 * H + 20
    sd = market.data["EURUSD"]
    for b in bars[-3:]:                              # the bridge's normal push: last 3 bars
        sd.h1.add(b)
    sd.quotes.append(Quote("EURUSD", now - 1, close - 0.00005, close + 0.00005))
    vb.tick(now, {"EURUSD": sd.quotes[-1]})
    assert vb.latest["EURUSD"]["status"] == "waiting" and vb.latest["EURUSD"]["bars"] == 3
    for b in bars:                                   # then the history backfill
        sd.h1.add(b)
    vb.tick(now + 30, {"EURUSD": sd.quotes[-1]})
    assert vb.latest["EURUSD"]["bars"] == len(bars)
    assert vb.latest["EURUSD"]["status"] == "entered"
