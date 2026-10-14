import math

import pytest

from jevsys.config import StateConfig
from jevsys.fx_state import FxStateEngine, estimate_tokens, session_at, _rsi
from jevsys.market import MarketStore
from jevsys.sim_fx import SimBridge
from jevsys.types import BAR_SECONDS

T0 = 1_760_000_000.0 - 1_760_000_000.0 % 86_400  # midnight UTC


def loaded(days=5, seed=3):
    sim = SimBridge(seed=seed, start_ts=T0)
    m = MarketStore()
    now = T0 + days * 86_400 + 2
    m.ingest(sim.step_to(now), now)
    return m, sim, now


def flat_bar(ts, price, spread_pts=10):
    return [ts, price, price, price, price, 100, spread_pts]


# ---- snapshot shape --------------------------------------------------------
def test_every_instrument_snapshot_is_numeric_and_small():
    m, _, now = loaded()
    eng = FxStateEngine(m)
    for sym in m.data:
        s = eng.snapshot(sym, now)
        assert estimate_tokens(s) <= 400
        for k, v in s.items():
            assert v is None or (isinstance(v, (int, float)) and math.isfinite(v)), (sym, k, v)
        assert s["atr_pips"] > 0 and 0 <= s["rsi14"] <= 100
        assert s["bars_24h"] == 288
        assert FxStateEngine.is_fresh(s, eng.cfg)[0]
    assert eng.snapshot("XAUUSD", now)["gold"] == 1 and eng.snapshot("EURUSD", now)["gold"] == 0


def test_missing_history_gives_none_not_guesses():
    sim = SimBridge(seed=1, start_ts=T0)
    m = MarketStore()
    now = T0 + 3 * 3600 + 2  # 3 hours only
    m.ingest(sim.step_to(now), now)
    s = FxStateEngine(m).snapshot("EURUSD", now)
    assert s["ret_1d_bps"] is None and s["ema100_dist_atr"] is None and s["atr_vs_5d"] is None
    assert s["ret_1h_bps"] is not None


# ---- lookahead guard -------------------------------------------------------
def test_snapshot_is_causal():
    """The snapshot for time t must not change when later data arrives."""
    sim = SimBridge(seed=9, start_ts=T0)
    m = MarketStore()
    t = T0 + 4 * 86_400 + 2
    m.ingest(sim.step_to(t), t)
    eng = FxStateEngine(m)
    before = {s: eng.snapshot(s, t) for s in m.data}
    later = t + 6 * 3600
    m.ingest(sim.step_to(later), later)  # 6 more hours of bars and a newer quote
    after = {s: eng.snapshot(s, t) for s in m.data}
    assert before == after


def test_unclosed_and_future_data_rejected():
    m = MarketStore()
    now = T0 + 1_000
    forming = T0 + 900  # opens at 900, closes at 1200 > now
    res = m.ingest({"symbols": {"EURUSD": {"quote": {"t": now + 60, "bid": 1.1, "ask": 1.1001},
                                           "bars": [flat_bar(T0, 1.1), flat_bar(forming, 1.1)]}}}, now)
    assert len(m.data["EURUSD"].bars) == 1
    assert not m.data["EURUSD"].quotes
    assert any("unclosed" in r for r in res["rejected"]) and any("future" in r for r in res["rejected"])


def test_malformed_bar_rejected_and_revised_bar_replaced():
    m = MarketStore()
    now = T0 + 10_000
    m.ingest({"symbols": {"EURUSD": {"bars": [[T0, 1.1, 1.0, 1.2, 1.1, 1, 1]]}}}, now)  # high < low
    assert not m.data["EURUSD"].bars
    m.ingest({"symbols": {"EURUSD": {"bars": [flat_bar(T0, 1.1)]}}}, now)
    m.ingest({"symbols": {"EURUSD": {"bars": [flat_bar(T0, 1.2)]}}}, now)
    assert len(m.data["EURUSD"].bars) == 1 and m.data["EURUSD"].bars[0].close == 1.2


def test_need_history_until_backfilled():
    m = MarketStore()
    assert "XAUUSD" in m.ingest({"symbols": {}}, T0)["need_history"]
    m2, _, _ = loaded(days=2)
    assert m2.ingest({"symbols": {}}, T0)["need_history"] == []


# ---- staleness (guide: never act on stale data) ----------------------------
def test_stale_quote_is_not_fresh():
    m, _, now = loaded(days=2)
    eng = FxStateEngine(m)
    later = now + 3_600  # an hour with no data: weekend / bridge down
    s = eng.snapshot("XAUUSD", later)
    fresh, why = FxStateEngine.is_fresh(s, eng.cfg)
    assert not fresh and "old" in why


# ---- the math --------------------------------------------------------------
def test_spread_pips_and_vs_normal():
    m = MarketStore()
    now = T0 + 301
    m.ingest({"symbols": {"USDJPY": {"point": 0.001, "quote": {"t": now - 1, "bid": 150.000, "ask": 150.020},
                                     "bars": [flat_bar(T0, 150.0, spread_pts=10)]}}}, now)
    s = FxStateEngine(m).snapshot("USDJPY", now)
    assert s["spread_pips"] == 2.0          # 0.02 / 0.01 pip
    assert s["spread_vs_norm"] == 2.0       # normal = 10 points = 1.0 pip


def test_returns_and_usd_index_signs():
    m = MarketStore()
    rows = lambda p0, p1: [flat_bar(T0 + i * 300, p0) for i in range(12)] + [flat_bar(T0 + 12 * 300, p1)]  # noqa: E731
    now = T0 + 13 * 300 + 1
    # dollar strengthens: EUR, GBP, AUD fall 10 bps; USDJPY rises 10 bps
    m.ingest({"symbols": {
        "EURUSD": {"bars": rows(1.1, 1.1 * 0.999)}, "GBPUSD": {"bars": rows(1.3, 1.3 * 0.999)},
        "AUDUSD": {"bars": rows(0.66, 0.66 * 0.999)}, "USDJPY": {"bars": rows(150.0, 150.0 * 1.001)}}}, now)
    eng = FxStateEngine(m)
    assert eng.snapshot("EURUSD", now)["ret_1h_bps"] == pytest.approx(-10, abs=0.01)
    assert eng.usd_index(now, 3600) == pytest.approx(10, abs=0.01)


def test_rsi_extremes():
    assert _rsi([float(i) for i in range(40)], 14) == 100.0
    assert _rsi([float(40 - i) for i in range(40)], 14) == 0.0
    assert _rsi([1.0] * 20, 14) == 50.0


@pytest.mark.parametrize("hour,code", [(3, 0), (8, 1), (13, 2), (18, 3), (22, 4)])
def test_sessions(hour, code):
    assert session_at(T0 + hour * 3600)[0] == code


def test_budget_enforced():
    m, _, now = loaded(days=1)
    with pytest.raises(ValueError):
        FxStateEngine(m, StateConfig(max_snapshot_tokens=30)).snapshot("EURUSD", now)
