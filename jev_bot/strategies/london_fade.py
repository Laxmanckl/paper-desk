"""H20: fade the London-open breakout (found in H19's results; judged on sealed data only).

The exact mirror of H19 (london_orb.py): when the first 1-hour close breaks London's
opening-hour range, trade the OTHER way.
    entry    above the range: SELL; below: BUY (once per day, within 4 hours)
    exits    target the middle of the range; stop 1.5x that distance beyond the entry;
             out after 6 bars. Chance for this shape: 60%.

Why it might work: H19's breakouts reversed through the range middle far more often
than chance (36% vs 42%, z -5.1, 2,099 trades, 2025-26). Early London breaks may be
liquidity runs on the stops just outside the first hour's range that then snap back.
"""

from __future__ import annotations

from .london_orb import LondonORB


class LondonFade(LondonORB):
    def signal(self, history):
        side = super().signal(history)
        return {"BUY": "SELL", "SELL": "BUY"}.get(side)

    def levels(self, history, side, entry, atr_now):
        r = self._range(history)
        if not r:
            return None
        mid = (r[0] + r[1]) / 2
        to_mid = mid - entry if side == "BUY" else entry - mid
        return (1.5 * to_mid, to_mid) if to_mid > 0 else None

    def tests(self):
        flip = {"BUY": "SELL", "SELL": "BUY", None: None}
        return [(d.replace("buys", "SELLS (fade)").replace("sells", "BUYS (fade)"), h, flip[w])
                for d, h, w in super().tests()]


STRATEGY = LondonFade(
    name="london_fade", version=1, timeframe="H1", warmup=150, history_bars=160, needs_real_dates=True,
    hypothesis="The first 1-hour close beyond London's opening-hour range returns to the range middle "
               "before running 1.5x that distance further, more often than the 60% chance rate",
    markets=("XAUUSD", "EURUSD"), risk_pct=0.01, sl_atr=1.5, tp_atr=1.0, max_hold=6, max_leverage=10.0)
