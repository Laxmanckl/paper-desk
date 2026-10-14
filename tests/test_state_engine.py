import math

import pytest

from jevsys.book_state import AccountState
from jevsys.feeds.sim import SimFeed
from jevsys.state_engine import LookaheadError, SnapshotTooLarge, StateEngine, estimate_tokens
from jevsys.config import StateConfig
from jevsys.types import BlockInput, Fill, Level, OrderBook, OwnQuote, Trade

T0 = 1_760_000_000.0


def book(block, ts, bid=99.0, ask=101.0, bsz=(10, 20, 30), asz=(10, 20, 30)):
    return OrderBook(block, ts,
                     tuple(Level(bid - i, s) for i, s in enumerate(bsz)),
                     tuple(Level(ask + i, s) for i, s in enumerate(asz)))


# ---- core numbers -------------------------------------------------------
def test_price_and_book_features():
    eng = StateEngine()
    s = eng.on_block(BlockInput(book=book(1, T0, bsz=(30, 20, 10), asz=(10, 20, 30))))
    assert s["mid"] == 100.0
    assert s["spread_bps"] == 200.0
    # microprice leans toward the thin side (ask): (99*10 + 101*30)/40 = 100.5
    assert s["micro_bps"] == 50.0
    assert s["imb"] == 0.0  # 60 vs 60 over top 3
    assert (s["bd1"], s["ad3"]) == (30, 30)


def test_imbalance_sign():
    s = StateEngine().on_block(BlockInput(book=book(1, T0, bsz=(90, 0.1, 0.1), asz=(10, 0.1, 0.1))))
    assert s["imb"] > 0.7


def test_returns_use_past_mid_only():
    eng = StateEngine()
    eng.on_block(BlockInput(book=book(1, T0, 99, 101)))           # mid 100
    s = eng.on_block(BlockInput(book=book(2, T0 + 60, 100, 102)))  # mid 101, 60s later
    assert s["ret_1m_bps"] == pytest.approx(100.0)
    assert s["ret_5m_bps"] is None  # not enough history yet: None, never invented


def test_flow_window():
    eng = StateEngine()
    trades = [Trade(T0 - 1, 101, 6, "buy"), Trade(T0 - 2, 99, 2, "sell"), Trade(T0 - 50, 101, 99, "buy")]
    s = eng.on_block(BlockInput(book=book(1, T0), trades=trades, cancels=20))
    assert s["agg_buy"] == 6 and s["agg_sell"] == 2      # 50s-old trade outside 10s window
    assert s["agg_buy_ratio"] == 0.75
    assert s["trades_ps"] == 0.2 and s["cancels_ps"] == 2.0


def test_queue_position():
    q = [OwnQuote("buy", 99, 5, size_ahead=15), OwnQuote("sell", 101, 5, size_ahead=0)]
    s = StateEngine().on_block(BlockInput(book=book(1, T0), own_quotes=q))
    assert s["q_bid"] == 0.75 and s["q_ask"] == 0.0


def test_stale_reference_price_dropped():
    s = StateEngine().on_block(BlockInput(book=book(1, T0), ref_price=99.0, ref_ts=T0 - 30))
    assert s["ref_gap_bps"] is None
    s2 = StateEngine().on_block(BlockInput(book=book(1, T0), ref_price=99.0, ref_ts=T0 - 1))
    assert s2["ref_gap_bps"] == pytest.approx((100 / 99 - 1) * 1e4, abs=0.01)


# ---- lookahead guard (guide: "no future data") --------------------------
@pytest.mark.parametrize("bad", [
    lambda: BlockInput(book=book(2, T0), trades=[Trade(T0 + 0.01, 101, 1, "buy")]),
    lambda: BlockInput(book=book(2, T0), fills=[Fill(T0 + 1, "buy", 101, 1, 101)]),
    lambda: BlockInput(book=book(2, T0), ref_price=100, ref_ts=T0 + 0.5),
])
def test_future_inputs_rejected(bad):
    eng = StateEngine()
    eng.on_block(BlockInput(book=book(1, T0 - 1)))
    with pytest.raises(LookaheadError):
        eng.on_block(bad())


def test_out_of_order_blocks_rejected():
    eng = StateEngine()
    eng.on_block(BlockInput(book=book(5, T0)))
    with pytest.raises(LookaheadError):
        eng.on_block(BlockInput(book=book(4, T0 + 1)))
    with pytest.raises(LookaheadError):
        eng.on_block(BlockInput(book=book(6, T0 - 1)))


def test_snapshot_is_causal():
    """Snapshot at block N must be identical whether or not later blocks exist."""
    blocks = list(SimFeed(n_blocks=800, seed=3).blocks())
    a, b = StateEngine(), StateEngine()
    snaps_a = [a.on_block(x) for x in blocks[:500]]
    snaps_b = [b.on_block(x) for x in blocks]
    assert snaps_a == snaps_b[:500]


def test_crossed_book_rejected():
    with pytest.raises(ValueError):
        StateEngine().on_block(BlockInput(book=book(1, T0, bid=101, ask=101)))


# ---- token budget / numbers only ----------------------------------------
def test_budget_and_numeric_only_over_long_run():
    eng = StateEngine()
    for blk in SimFeed(n_blocks=8000, seed=11).blocks():
        s = eng.on_block(blk)
        assert estimate_tokens(s) <= 400
        for k, v in s.items():
            if k == "lat_ms":
                assert all(isinstance(x, (int, float)) for x in v)
            else:
                assert v is None or (isinstance(v, (int, float)) and math.isfinite(v)), (k, v)
    assert s["vol_s_bps"] is not None and s["vol_m_bps"] is not None and s["ret_30m_bps"] is not None


def test_budget_enforced():
    eng = StateEngine(StateConfig(max_snapshot_tokens=20))
    with pytest.raises(SnapshotTooLarge):
        eng.on_block(BlockInput(book=book(1, T0)))


def test_latency_hook_keeps_last_10():
    eng = StateEngine()
    for i in range(15):
        eng.record_latency(100 + i)
    s = eng.on_block(BlockInput(book=book(1, T0)))
    assert s["lat_ms"] == list(range(105, 115))


# ---- our book -----------------------------------------------------------
def test_account_open_add_reduce_flip():
    a = AccountState(1000)
    a.apply_fill(Fill(1, "buy", 100, 2, 100))
    a.apply_fill(Fill(2, "buy", 110, 2, 110))
    assert a.position == 4 and a.avg_entry == 105
    a.apply_fill(Fill(3, "sell", 115, 1, 115))
    assert a.position == 3 and a.realized_pnl == 10
    a.apply_fill(Fill(4, "sell", 100, 5, 100))   # close 3 @ -5 each, open short 2 @ 100
    assert a.realized_pnl == -5 and a.position == -2 and a.avg_entry == 100
    assert a.position_opened_ts == 4
    assert a.unrealized(90) == 20


def test_drawdown_and_holding_time_in_snapshot():
    eng = StateEngine(starting_equity=1000)
    eng.on_block(BlockInput(book=book(1, T0, 99, 101), fills=[Fill(T0, "buy", 100, 10, 100)],
                            orders_sent=2))
    s = eng.on_block(BlockInput(book=book(2, T0 + 30, 89, 91)))  # mid 90 -> -100
    assert s["inv"] == 10 and s["upnl"] == -100.0
    assert s["dd"] == pytest.approx(0.1)
    assert s["held_s"] == 30.0
    assert s["fill_ratio"] == 0.5


def test_slippage_sign():
    eng = StateEngine()
    s = eng.on_block(BlockInput(book=book(1, T0), fills=[Fill(T0, "buy", 100.1, 1, 100.0)], orders_sent=1))
    assert s["slip_bps"] == pytest.approx(10.0)  # paid up = positive (bad)
