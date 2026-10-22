"""Volume-confirmed London breakout (``volume_breakout``).

Built on ``london_breakout``, the only strategy in this repo whose untouched
TEST window beat random entries (PF 1.60, 0.4% of random runs better).  Three
filters are added, each with a reason, and volume is the main one:

* **Volume (RVOL)** - the breakout bar's tick volume must be at least
  ``min_rvol`` x the average volume of that same hour on the previous
  ``rvol_lookback_days`` days (``bot.intraday.relative_volume``).  A range
  break that nobody trades is the classic false breakout.
* **VWAP side** - longs only when the breakout close is above the
  volume-weighted average price anchored at the start of the *previous* UTC
  day (``vwap_anchor="previous_day"``), shorts only below it: the
  volume-weighted crowd of the last day and a half is already leaning the way
  of the trade.  (A VWAP anchored at today's open is useless here: a first
  close above the Asian high is above every price traded today, so it would
  pass every long.)
* **Volatility regime** - ATR14 must be at least ``atr_regime_min`` x its own
  ``atr_regime_days``-day average, i.e. trade expanding (volatile) markets,
  stand aside in dead ones.

Everything else is the London breakout: Asian range 00:00-07:00 UTC, the first
close outside it between 07:00 and 11:00 is the setup (if that bar fails a
filter the day is spent, the bot does not chase a later break), daily EMA50
trend gate from the previous completed day, stop at the far side of the range
capped at ``sl_cap_atr`` x ATR, target ``tp_r`` x R, forced close 20:00 UTC.

A feed without volume produces **no trades** (and says so) rather than
pretending: RVOL and VWAP are meaningless on yfinance forex zeros.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .decisions import entry_text
from .indicators import atr
from .intraday import (
    bars_per_hour,
    daily_bars,
    daily_state,
    day_ids,
    hour_array,
    map_prev_daily,
    relative_volume,
    session_range,
    volume_array,
)
from .plans import EntryPlan, empty_plans
from .strategy_vwap import anchored_vwap

__all__ = ["prepare", "signals", "build_plans", "GRID"]

# 8 combinations, tuned on TRAIN only
GRID: list[dict] = [
    {"tp_r": tp, "min_rvol": rv, "sl_cap_atr": cap}
    for tp in (1.5, 2.0)
    for rv in (1.2, 1.5)
    for cap in (2.0, 3.0)
]


def build_plans(df: pd.DataFrame, params, daily: pd.DataFrame | None = None,
                explain: bool = False) -> tuple[np.ndarray, dict]:
    n = len(df)
    plans = empty_plans(n)
    if n == 0:
        return plans, {}

    hours = hour_array(df.index)
    atr_bar = atr(df, params.atr_period).to_numpy(dtype=float)
    range_hi, range_lo = session_range(
        df, params.range_start_utc, params.range_end_utc,
        visible_from_hour=params.range_end_utc,
    )
    width = range_hi - range_lo
    bph = bars_per_hour(df.index)
    horizon_atr = atr_bar * np.sqrt(float(params.range_atr_horizon) * bph)
    range_ok = (
        np.isfinite(width) & np.isfinite(horizon_atr) & (horizon_atr > 0)
        & (width >= params.range_atr_min * horizon_atr)
        & (width <= params.range_atr_max * horizon_atr)
    )

    close = df["close"].to_numpy(dtype=float)
    in_window = (hours >= params.entry_start_utc) & (hours < params.entry_end_utc)
    beyond_up = in_window & (close > range_hi)
    beyond_dn = in_window & (close < range_lo)
    ids = day_ids(df.index)
    any_break = (beyond_up | beyond_dn).astype(int)
    counts = pd.Series(any_break).groupby(ids).cumsum().to_numpy()
    first = (any_break == 1) & (counts == 1)

    has_volume = volume_array(df) is not None
    rvol = relative_volume(df, params.rvol_lookback_days)
    vwap = (anchored_vwap(df, params.vwap_anchor, 0, 24).to_numpy(dtype=float)
            if has_volume else np.full(n, np.nan))
    regime_bars = max(int(round(params.atr_regime_days * 24 * bph)), 2)
    # needs at least 5 days of bars before the regime is known
    min_bars = max(int(round(5 * 24 * bph)), 2)
    atr_mean = pd.Series(atr_bar).rolling(regime_bars, min_periods=min(min_bars, regime_bars)).mean()
    atr_ratio = atr_bar / atr_mean.to_numpy(dtype=float)

    daily_frame = daily if daily is not None and len(daily) else daily_bars(df)
    dstate = daily_state(daily_frame, params.daily_ema_period, params.atr_period)
    above = map_prev_daily(df.index, dstate, "above_ema")
    trend = np.where(np.isfinite(above), np.where(above, 1, -1), 0)

    skips = {"no_volume_feed": 0, "range_size": 0, "daily_trend": 0, "low_volume": 0,
             "vwap_side": 0, "quiet_regime": 0, "bad_stop": 0}
    why = {}
    for i in np.flatnonzero(first):
        direction = 1 if beyond_up[i] else -1
        if not has_volume:
            skips["no_volume_feed"] += 1
            why[i] = "no entry - this feed has no volume (volume rules need MT5 tick volume)"
            continue
        if not range_ok[i]:
            skips["range_size"] += 1
            why[i] = "no entry - Asian range too small/large vs ATR"
            continue
        if params.require_daily_trend and trend[i] != direction:
            skips["daily_trend"] += 1
            why[i] = "no entry - breakout against the daily EMA trend"
            continue
        if not (np.isfinite(rvol[i]) and rvol[i] >= params.min_rvol):
            skips["low_volume"] += 1
            why[i] = (f"no entry - breakout on low volume (RVOL "
                      f"{rvol[i]:.2f} < {params.min_rvol:g})")
            continue
        if params.require_vwap_side and not (
                np.isfinite(vwap[i]) and (close[i] - vwap[i]) * direction > 0):
            skips["vwap_side"] += 1
            why[i] = "no entry - breakout on the wrong side of today's VWAP"
            continue
        if not (np.isfinite(atr_ratio[i]) and atr_ratio[i] >= params.atr_regime_min):
            skips["quiet_regime"] += 1
            why[i] = (f"no entry - quiet market (ATR {atr_ratio[i]:.2f}x its "
                      f"{params.atr_regime_days}-day average)")
            continue
        atr_i = atr_bar[i]
        opposite = close[i] - range_lo[i] if direction > 0 else range_hi[i] - close[i]
        sl_dist = float(min(opposite, params.sl_cap_atr * atr_i))
        if not np.isfinite(sl_dist) or sl_dist <= 0:
            skips["bad_stop"] += 1
            why[i] = "no entry - stop distance invalid"
            continue
        tp_dist = float(params.tp_r) * sl_dist if params.tp_r is not None else None
        plans[i] = EntryPlan(direction=direction, sl_dist=sl_dist, tp_dist=tp_dist,
                             note=f"volume_breakout rvol={rvol[i]:.2f}")

    meta: dict = dict(skips)
    meta["rvol"] = rvol
    if explain:
        out = np.full(n, "", dtype=object)
        for i in range(n):
            if plans[i] is not None:
                out[i] = entry_text(plans[i])
            elif i in why:
                out[i] = why[i]
            elif not in_window[i]:
                out[i] = "no entry - outside the London entry window"
            elif counts[i] == 0:
                out[i] = "no entry - no close outside the Asian range yet"
            else:
                out[i] = "no entry - range already broken today (first break only)"
        meta["reasons"] = out
    return plans, meta


def prepare(df: pd.DataFrame, params, daily: pd.DataFrame | None = None,
            explain: bool = False) -> pd.DataFrame:
    out = df.copy()
    out["atr"] = atr(out, params.atr_period)
    plans, meta = build_plans(out, params, daily, explain=explain)
    out["plan"] = plans
    out["rvol"] = meta.pop("rvol", np.full(len(out), np.nan))
    out.attrs["volume_breakout_meta"] = meta
    return out


def signals(df: pd.DataFrame, params) -> pd.Series:
    if "plan" not in df.columns:
        df = prepare(df, params)
    out = np.zeros(len(df), dtype=int)
    for i, p in enumerate(df["plan"].to_numpy()):
        if p is not None:
            out[i] = p.direction
    return pd.Series(out, index=df.index, dtype=int)
