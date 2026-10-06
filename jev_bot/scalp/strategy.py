"""The scalping rule: buy short pullbacks inside a 1-minute uptrend (and sell
short rallies inside a downtrend), with a tight stop and a quick target.

On every closed 1-minute candle, per instrument:

  trend     EMA(20) above EMA(50) and price above EMA(50)      -> only longs
            EMA(20) below EMA(50) and price below EMA(50)      -> only shorts
  pullback  RSI(7) dipped below 30 in the last 3 candles and has
            turned back up through 40 on this candle            -> BUY
            (mirror: above 70, back down through 60             -> SELL)
  costs     the round-trip cost (spread + fees both ways) must be at most
            25% of the risk: the stop is widened to make that true, and the
            trade is skipped if that would need a stop wider than 4 x ATR
  quiet     skip if the market is barely moving (ATR tiny vs price)

Exits (checked on every price update, roughly every second):
  stop   = 1.2 x ATR(14, 1-min) from entry (wider if costs require it)
  target = 1.5 x the stop distance
  time   = close after 30 minutes if neither was hit
"""

from __future__ import annotations

from dataclasses import dataclass

from .bars import Bar, atr, ema, rsi


@dataclass
class ScalpConfig:
    ema_fast: int = 20
    ema_slow: int = 50
    rsi_len: int = 7
    rsi_low: float = 30.0
    rsi_high: float = 70.0
    rsi_reset: float = 10.0          # must turn back by this much (30 -> 40)
    lookback: int = 3
    stop_atr: float = 1.2
    reward_risk: float = 1.5         # target distance = 1.5 x stop distance
    max_minutes: int = 30
    max_cost_frac: float = 0.25      # round-trip costs <= 25% of the risk
    max_stop_atr: float = 4.0        # ...but never a stop wider than 4 x ATR
    crypto_fee: float = 0.0005       # per side; Binance futures taker ~0.05%, spot 0.1%
    fx_commission_per_100k: float = 0.0   # per side; 0 for standard MT5 accounts
    min_atr_frac: float = 0.00005    # ATR at least 0.005% of price, else too quiet
    risk_frac: float = 0.0025        # risk 0.25% of equity per trade
    cooldown_min: int = 5            # wait after a trade closes on that instrument
    max_trades_per_day: int = 20     # per instrument
    max_open: int = 5                # across everything
    daily_loss_halt: float = 0.02    # stop opening trades after -2% in a UTC day
    fx_session_utc: tuple = (6, 20)  # forex/gold: trade 06:00-20:00 UTC only


@dataclass
class Signal:
    action: str | None     # "BUY", "SELL" or None
    atr: float
    trend: str             # "up", "down" or "flat"
    rsi: float
    reason: str            # why there is (or isn't) a trade


def evaluate(bars: list[Bar], cfg: ScalpConfig) -> Signal:
    need = cfg.ema_slow + 5
    if len(bars) < need:
        return Signal(None, 0.0, "flat", 50.0, f"warming up ({len(bars)}/{need} candles)")
    closes = [b.c for b in bars]
    ef, es = ema(closes, cfg.ema_fast)[-1], ema(closes, cfg.ema_slow)[-1]
    r = rsi(closes, cfg.rsi_len)
    a = atr(bars, 14)
    px = closes[-1]
    trend = "up" if ef > es and px > es else "down" if ef < es and px < es else "flat"
    recent = r[-1 - cfg.lookback:-1]
    if a < px * cfg.min_atr_frac:
        return Signal(None, a, trend, r[-1], "market too quiet")
    if trend == "up" and min(recent) < cfg.rsi_low and r[-1] >= cfg.rsi_low + cfg.rsi_reset:
        return Signal("BUY", a, trend, r[-1], "pullback in uptrend turned up")
    if trend == "down" and max(recent) > cfg.rsi_high and r[-1] <= cfg.rsi_high - cfg.rsi_reset:
        return Signal("SELL", a, trend, r[-1], "rally in downtrend turned down")
    if trend == "flat":
        return Signal(None, a, trend, r[-1], "no clear trend")
    return Signal(None, a, trend, r[-1], f"waiting for a {'pullback' if trend == 'up' else 'rally'}")
