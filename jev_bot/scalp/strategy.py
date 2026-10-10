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
  15-min    the bigger trend must agree: EMA(20) vs EMA(50) on 15-minute
            candles must point the same way as the 1-minute trend
  spread    skip if the spread is over 1.5 x its normal level (last hour)
  gold      no new gold trades 21:00-06:00 UTC (thin, jumpy Asian session)

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
    # forex/gold trade round the clock (Sun 22:00 - Fri 21:00 UTC), except no NEW trades
    # during the daily rollover, when brokers' spreads jump for a few minutes.
    # (start, end) in UTC hours; () = no pause.  20.75 = 20:45.
    fx_pause_utc: tuple = (20.75, 22.0)
    metal_pause_utc: tuple = (21.0, 6.0)   # gold: no new trades 21:00-06:00 UTC; () = off
    spread_guard: float = 1.5        # skip if spread > 1.5 x its normal (median of last hour); 0 = off
    htf_minutes: int = 15            # bigger-trend filter: EMA(20)/EMA(50) on 15-min candles
    htf_fast: int = 20
    htf_slow: int = 50               # 0 = filter off
    strategy: str = "pullback"       # "pullback" (A) or "orb" (B: opening-range breakout)
    # --- B: opening-range breakout (only used when strategy == "orb") ---
    orb_range_utc: tuple = (6.0, 7.0)      # the range: high/low of 06:00-07:00 UTC
    orb_entry_until: float = 12.0          # breakouts only until 12:00 UTC
    orb_exit_utc: float = 16.0             # anything still open closes at 16:00 UTC
    orb_buffer: float = 0.1                # close must clear the range by 10% of its height

    @classmethod
    def from_dict(cls, d: dict) -> "ScalpConfig":
        """Rebuild from a saved account, ignoring settings older versions had."""
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


def hiwin_config(**kw) -> ScalpConfig:
    """Scalper C, built for a high win rate: the same pullback entries as A, but a
    small target (0.4 x the stop) and a wide stop (2.5 x ATR), up to 60 minutes.
    A win is worth 0.4 of a loss, so it must win over ~71% just to break even,
    and costs must stay under 10% of the risk (else the tiny target cannot pay them)."""
    base = dict(reward_risk=0.4, stop_atr=2.5, max_stop_atr=6.0, max_cost_frac=0.10, max_minutes=60)
    base.update(kw)
    return ScalpConfig(**base)


@dataclass
class Signal:
    action: str | None     # "BUY", "SELL" or None
    atr: float
    trend: str             # "up", "down", "flat" (or "" when not used)
    rsi: float | None
    reason: str            # why there is (or isn't) a trade
    stop: float | None = None       # absolute stop price (else the ATR rule)
    deadline: float | None = None   # unix time to close by (else max_minutes)


def htf_closes(bars: list[Bar], minutes: int) -> list[float]:
    """Closes of the completed N-minute candles inside a list of 1-minute candles."""
    step = minutes * 60
    out, last_block = [], None
    for b in bars:
        blk = b.t // step
        if last_block is not None and blk != last_block:
            out.append(prev_c)
        last_block, prev_c = blk, b.c
    if bars and (bars[-1].t + 60) % step == 0:       # the newest bar finished its block
        out.append(bars[-1].c)
    return out


def htf_trend(bars: list[Bar], cfg: ScalpConfig) -> str | None:
    """"up"/"down"/"flat" on the bigger timeframe, or None while there is too little history."""
    c = htf_closes(bars, cfg.htf_minutes)
    if len(c) < cfg.htf_slow:
        return None
    f, s = ema(c, cfg.htf_fast)[-1], ema(c, cfg.htf_slow)[-1]
    return "up" if f > s else "down" if f < s else "flat"


def evaluate(bars: list[Bar], cfg: ScalpConfig, t: float = 0.0, st: dict | None = None) -> Signal:
    if cfg.strategy == "orb":
        from .orb import evaluate as orb_eval
        return orb_eval(bars, cfg, t, st if st is not None else {})
    need = cfg.ema_slow + 5
    if len(bars) < need:
        return Signal(None, 0.0, "flat", 50.0, f"warming up ({len(bars)}/{need} candles)")
    closes = [b.c for b in bars[-300:]]
    ef, es = ema(closes, cfg.ema_fast)[-1], ema(closes, cfg.ema_slow)[-1]
    r = rsi(closes, cfg.rsi_len)
    a = atr(bars, 14)
    px = closes[-1]
    trend = "up" if ef > es and px > es else "down" if ef < es and px < es else "flat"
    recent = r[-1 - cfg.lookback:-1]
    if a < px * cfg.min_atr_frac:
        return Signal(None, a, trend, r[-1], "market too quiet")
    big = htf_trend(bars, cfg) if cfg.htf_slow else trend
    if big is None:
        need_m = cfg.htf_minutes * cfg.htf_slow
        return Signal(None, a, trend, r[-1], f"collecting 15-min trend history ({len(bars)}/{need_m} min)")
    if trend in ("up", "down") and big != trend:
        return Signal(None, a, trend, r[-1], f"1-min trend {trend}, 15-min trend {big}: waiting until they agree")
    if trend == "up" and min(recent) < cfg.rsi_low and r[-1] >= cfg.rsi_low + cfg.rsi_reset:
        return Signal("BUY", a, trend, r[-1], "pullback in uptrend turned up")
    if trend == "down" and max(recent) > cfg.rsi_high and r[-1] <= cfg.rsi_high - cfg.rsi_reset:
        return Signal("SELL", a, trend, r[-1], "rally in downtrend turned down")
    if trend == "flat":
        return Signal(None, a, trend, r[-1], "no clear trend")
    return Signal(None, a, trend, r[-1], f"waiting for a {'pullback' if trend == 'up' else 'rally'}")
