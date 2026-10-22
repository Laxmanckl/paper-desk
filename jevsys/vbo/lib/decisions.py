"""Decision logging: one plain-English line per closed bar per symbol.

The live runner must answer "why did the bot not trade the 09:00 bar?" without
a debugger.  The strategies already know the answer — they evaluate exactly the
conditions that gate an entry — so instead of re-deriving them here, the
strategy modules record a reason per bar when ``prepare(..., explain=True)`` is
called, and this module formats those reasons into the log line:

    EURUSD 1h 08:00 UTC: no entry - Asian range 0.42 ATR < range_atr_min 1.0

Re-deriving the conditions in a second place would be a bug farm: the log would
silently drift away from what the strategy actually did.  The non-plan
strategies (``trend_pullback``, ``bollinger_reversion``, ``vwap_ema_cross``)
have no per-bar reason array, so their conditions are evaluated here from the
indicator columns, in the same order the signal function applies them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "hhmm",
    "stamp",
    "window_label",
    "entry_text",
    "decision_line",
    "explain_bar",
]


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------
def _clock(hour: float) -> str:
    """07 -> '07', 13.5 -> '13:30'."""
    hour = float(hour)
    whole = int(np.floor(hour))
    minutes = int(round((hour - whole) * 60))
    if minutes >= 60:                      # 13.999 -> 14:00
        whole += 1
        minutes = 0
    return f"{whole:02d}" if minutes == 0 else f"{whole:02d}:{minutes:02d}"


def window_label(start: float, end: float) -> str:
    """UTC window for a log line: '07-11 UTC' or '13:30-14:30 UTC'."""
    return f"{_clock(start)}-{_clock(end)} UTC"


def hhmm(ts) -> str:
    return pd.Timestamp(ts).strftime("%H:%M")


def stamp(ts, timeframe: str) -> str:
    """Bar timestamp as it reads in the log (date for daily bars)."""
    ts = pd.Timestamp(ts)
    if str(timeframe).lower() in ("1d", "d1", "1day"):
        return ts.strftime("%Y-%m-%d")
    return f"{ts:%H:%M} UTC"


def entry_text(plan) -> str:
    """'ENTRY long - SL 0.045123, TP 1.5R (0.067684)' from an EntryPlan."""
    side = "long" if plan.direction > 0 else "short"
    if plan.tp_dist is not None and plan.sl_dist > 0:
        r_mult = float(plan.tp_dist) / float(plan.sl_dist)
        return (f"ENTRY {side} - SL {float(plan.sl_dist):.6g}, "
                f"TP {r_mult:.1f}R ({float(plan.tp_dist):.6g})")
    if plan.tp_price is not None:
        return (f"ENTRY {side} - SL {float(plan.sl_dist):.6g}, "
                f"TP {float(plan.tp_price):.6g}")
    return f"ENTRY {side} - SL {float(plan.sl_dist):.6g}, no target"


# ---------------------------------------------------------------------------
# per-bar explanation
# ---------------------------------------------------------------------------
def _entry_text_from_frame(df: pd.DataFrame, params, i: int, signal: int) -> str:
    """ENTRY text: the plan's own stop/target, else the ATR defaults."""
    plan = None
    if "plan" in df.columns:
        plan = df["plan"].iloc[i]
    if plan is not None and plan.direction == signal:
        return entry_text(plan)
    try:
        atr_v = float(df["atr"].iloc[i])
    except (KeyError, IndexError, TypeError, ValueError):
        atr_v = float("nan")
    if not np.isfinite(atr_v) or atr_v <= 0:
        return f"ENTRY {'long' if signal > 0 else 'short'}"
    sl = float(params.sl_atr) * atr_v
    side = "long" if signal > 0 else "short"
    if float(params.tp_atr) >= 1e8:
        return f"ENTRY {side} - SL {sl:.6g}, no target"
    tp = float(params.tp_atr) * atr_v
    r_mult = tp / sl if sl > 0 else float("nan")
    return f"ENTRY {side} - SL {sl:.6g}, TP {r_mult:.1f}R ({tp:.6g})"


def explain_bar(df: pd.DataFrame, params, i: int) -> str:
    """Reason text (without the symbol/time prefix) for bar ``i``."""
    name = getattr(params, "name", "")
    meta = df.attrs.get(f"{name}_meta") or {}
    reasons = meta.get("reasons")
    if reasons is not None and len(reasons) == len(df):
        text = reasons[i]
        return str(text) if text else "no entry - no signal"

    if name == "trend_pullback":
        return _trend_pullback_reason(df, params, i)
    if name == "bollinger_reversion":
        return _bollinger_reason(df, params, i)
    if name == "vwap_ema_cross":
        return _vwap_cross_reason(df, params, i)
    return "no entry - no signal"


def decision_line(symbol: str, timeframe: str, ts, df: pd.DataFrame,
                  params, i: int = -1, signal: int | None = None,
                  blocked: str | None = None) -> str:
    """Full decision line for one closed bar.

    ``signal`` is the strategy's direction for that bar (computed by the
    caller); ``blocked`` is why the live filters refused an entry that the
    strategy did signal.
    """
    ts = pd.Timestamp(df.index[i]) if ts is None else pd.Timestamp(ts)
    if signal is None:
        signal = int(df["signal"].iloc[i]) if "signal" in df.columns else 0
    signal = int(signal or 0)

    if signal != 0 and not blocked:
        text = _entry_text_from_frame(df, params, i, signal)
    elif signal != 0 and blocked:
        text = (f"no entry - {blocked} "
                f"(strategy signalled {'long' if signal > 0 else 'short'})")
    else:
        text = explain_bar(df, params, i)
    return f"{symbol} {timeframe} {stamp(ts, timeframe)}: {text}"


# ---------------------------------------------------------------------------
# legacy (signal-only) strategies
# ---------------------------------------------------------------------------
def _f(df: pd.DataFrame, i: int, column: str) -> float:
    try:
        return float(df[column].iloc[i])
    except (KeyError, IndexError, TypeError, ValueError):
        return float("nan")


def _trend_pullback_reason(df: pd.DataFrame, p, i: int) -> str:
    close = _f(df, i, "close")
    open_ = _f(df, i, "open")
    low, high = _f(df, i, "low"), _f(df, i, "high")
    ema_f, ema_s = _f(df, i, "ema_fast"), _f(df, i, "ema_slow")
    rsi_v, atr_v = _f(df, i, "rsi"), _f(df, i, "atr")
    window = f"EMA{p.ema_fast}/{p.ema_slow}"

    if not all(np.isfinite(v) for v in (close, ema_f, ema_s, rsi_v, atr_v)) or atr_v <= 0:
        return (f"no entry - indicators warming up "
                f"(need {max(p.ema_slow, p.rsi_period, p.atr_period)} bars)")

    if close > ema_s:
        long_side = True
    elif close < ema_s:
        long_side = False
    else:
        return f"no entry - no trend (close {close:.6g} at EMA{p.ema_slow})"

    if long_side:
        if rsi_v > p.rsi_long_max:
            return (f"no entry - RSI {rsi_v:.1f} above {p.rsi_long_max:g} "
                    f"(no pullback in an uptrend)")
        if p.require_pullback_touch:
            limit = ema_f + p.pullback_slack_atr * atr_v
            if low > limit:
                return (f"no entry - no pullback to EMA{p.ema_fast} "
                        f"(low {low:.6g} vs EMA+{p.pullback_slack_atr:g} ATR "
                        f"{limit:.6g})")
        if p.confirm_candle and close <= open_:
            return (f"no entry - need a bullish close "
                    f"(open {open_:.6g}, close {close:.6g})")
        if p.require_ema_stack and ema_f <= ema_s:
            return f"no entry - EMA{p.ema_fast} not above EMA{p.ema_slow} ({window})"
    else:
        if rsi_v < p.rsi_short_min:
            return (f"no entry - RSI {rsi_v:.1f} below {p.rsi_short_min:g} "
                    f"(no pullback in a downtrend)")
        if p.require_pullback_touch:
            limit = ema_f - p.pullback_slack_atr * atr_v
            if high < limit:
                return (f"no entry - no pullback to EMA{p.ema_fast} "
                        f"(high {high:.6g} vs EMA-{p.pullback_slack_atr:g} ATR "
                        f"{limit:.6g})")
        if p.confirm_candle and close >= open_:
            return (f"no entry - need a bearish close "
                    f"(open {open_:.6g}, close {close:.6g})")
        if p.require_ema_stack and ema_f >= ema_s:
            return f"no entry - EMA{p.ema_fast} not below EMA{p.ema_slow} ({window})"
    return "no entry - no signal (conditions passed but the bar did not qualify)"


def _bollinger_reason(df: pd.DataFrame, p, i: int) -> str:
    close = _f(df, i, "close")
    lower, mid, upper = _f(df, i, "bb_lower"), _f(df, i, "bb_mid"), _f(df, i, "bb_upper")
    ema_s = _f(df, i, "ema_slow")
    if not all(np.isfinite(v) for v in (close, lower, mid, upper)):
        return f"no entry - indicators warming up (need {p.bb_period} bars)"
    if p.use_trend_filter and np.isfinite(ema_s):
        if close > ema_s and close >= lower:
            return (f"no entry - close {close:.6g} inside the bands "
                    f"({lower:.6g} / {mid:.6g} / {upper:.6g})")
        if close < ema_s and close <= upper:
            return (f"no entry - close {close:.6g} inside the bands "
                    f"({lower:.6g} / {mid:.6g} / {upper:.6g})")
        if close < ema_s:
            return (f"no entry - close {close:.6g} below EMA{p.ema_slow} {ema_s:.6g} "
                    f"(long band touch in a downtrend)")
        return (f"no entry - close {close:.6g} above EMA{p.ema_slow} {ema_s:.6g} "
                f"(short band touch in an uptrend)")
    return (f"no entry - close {close:.6g} inside the bands "
            f"({lower:.6g} / {mid:.6g} / {upper:.6g})")


def _vwap_cross_reason(df: pd.DataFrame, p, i: int) -> str:
    close, ema_v = _f(df, i, "close"), _f(df, i, "ema")
    vwap_v, atr_v = _f(df, i, "vwap"), _f(df, i, "atr")
    if not all(np.isfinite(v) for v in (close, ema_v, vwap_v, atr_v)):
        if np.isfinite(ema_v) and np.isfinite(atr_v):
            return "no entry - VWAP unavailable (no volume in this feed)"
        return "no entry - indicators warming up (VWAP / EMA / ATR)"
    prev_close = _f(df, i - 1, "close") if i > 0 else float("nan")
    prev_ema = _f(df, i - 1, "ema") if i > 0 else float("nan")
    crossed_up = (close > ema_v) and np.isfinite(prev_ema) and prev_close <= prev_ema
    crossed_dn = (close < ema_v) and np.isfinite(prev_ema) and prev_close >= prev_ema
    if not (crossed_up or crossed_dn):
        return (f"no entry - no EMA{p.ema_period} cross "
                f"(close {close:.6g}, EMA {ema_v:.6g})")
    if crossed_up and close <= vwap_v:
        return (f"no entry - EMA cross up but close {close:.6g} below VWAP "
                f"{vwap_v:.6g}")
    if crossed_dn and close >= vwap_v:
        return (f"no entry - EMA cross down but close {close:.6g} above VWAP "
                f"{vwap_v:.6g}")
    return "no entry - no signal (conditions passed but the bar did not qualify)"
