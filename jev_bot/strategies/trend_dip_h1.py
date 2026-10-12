"""H14: trend dip with a small target, on 1-hour bars. Rules in _trend_dip.py."""

from __future__ import annotations

from ._trend_dip import TrendDip

STRATEGY = TrendDip(
    name="trend_dip_h1", version=1, timeframe="H1", warmup=210, history_bars=220,
    hypothesis="On 1-hour bars, an RSI(2) dip inside a 50/200 uptrend (mirror for downtrends) recovers 0.8 ATR "
               "before it falls 2 ATR at least 85% of the time",
    markets=("XAUUSD", "EURUSD"),          # tested on all 9 markets via the ledger entry
    risk_pct=0.01, sl_atr=2.0, tp_atr=0.8, max_hold=24, max_leverage=10.0)
