"""H13: the Lorentzian Classification indicator as a daily strategy, on any market.

Signals come straight from jev_bot/lorentzian.py (the Python port of jdehorty's
TradingView script). The script itself has no stop, target or sizing, so these are
added; its own exit, 4 candles after entry, is kept as the time limit:

    entry    the model's new long / short signal (filters and kernel agreeing)
    exits    out after 4 days (the model's horizon); an opposite signal reverses;
             protective stop 2 x ATR(14), far target 4 x ATR(14)
    sizing   1% of equity at risk on the stop

Warm-up: 300 days before the first trade (the regime filter needs 200).
"""

from __future__ import annotations

from ..fx import Bar
from ..lorentzian import Cached
from .base import DailyStrategy


class LorentzianDaily(DailyStrategy):
    def _model(self):
        if getattr(self, "_cache", None) is None:
            self._cache = Cached(lambda b: b.date, lambda b: (b.open, b.high, b.low, b.close))
        return self._cache

    def _out(self, history):
        return self._model().at(history) if history else None

    def signal(self, history):
        o = self._out(history)
        if not o:
            return None
        return "BUY" if o["start_long"] else "SELL" if o["start_short"] else None

    def conditions(self, history):
        o = self._out(history)
        return {k: o[k] for k in ("prediction", "signal", "volatility_ok", "regime_ok")} if o else {}

    def explain(self, history):
        o = self._out(history)
        if not o or not o["warm"]:
            return "model warming up"
        return (f"neighbour vote {o['prediction']:+d}, signal {o['signal']:+d}, volatility "
                f"{'ok' if o['volatility_ok'] else 'low'}, regime {'ok' if o['regime_ok'] else 'flat'}")

    def tests(self):
        flat = [Bar(f"d{i:04d}", 100, 100, 100, 100) for i in range(400)]
        return [("too little history does nothing", flat[:20], None),
                ("a dead flat market never trades (no volatility)", flat, None)]


STRATEGY = LorentzianDaily(
    name="lorentzian_daily", version=1,
    hypothesis="jdehorty's Lorentzian Classification (ported unchanged) predicts 4-day direction on daily candles",
    markets=("XAUUSD", "EURUSD"), risk_pct=0.01, sl_atr=2.0, tp_atr=4.0, max_hold=4, warmup=300)
