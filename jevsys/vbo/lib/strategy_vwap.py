"""Anchored-VWAP + EMA9 cross strategy (``vwap_ema_cross``).

Everything below is written out so the numbers can be audited by hand.

VWAP
----
::

    VWAP(t) = SUM(OHLC4_i * tick_volume_i) / SUM(tick_volume_i)
              for i from the ANCHOR BAR up to bar t (inclusive)

    OHLC4   = (open + high + low + close) / 4

The **anchor bar** depends on the timeframe:

``previous_day``  (1h)
    The first session bar of the *previous trading day*.  The cumulative
    sums therefore restart once per trading day, and each day's line
    includes the whole previous day.

``previous_week`` (4h)
    The first session bar of the *previous trading week*.  The cumulative
    sums restart once per trading week.

A **trading day** is a UTC calendar day that contains at least one bar
inside the session window.  A **trading week** is the ISO week of a trading
day.  Bars outside the session window never contribute to the VWAP sums.

EMA9
----
::

    ema(t) = ewm(span=9, adjust=False).mean() of close

ENTRY (signal on the close of bar t, fill at the open of bar t+1)
-----------------------------------------------------------------
::

    Long : close(t-1) <= ema(t-1)  AND  close(t) > ema(t)  AND  close(t) > vwap(t)
    Short: close(t-1) >= ema(t-1)  AND  close(t) < ema(t)  AND  close(t) < vwap(t)

EXIT (evaluated on bars strictly after the entry bar)
-----------------------------------------------------
1. ``sl``        - server-side hard stop at N x ATR from the entry price.
2. ``tp``        - optional take profit at K x R (R = the stop distance).
3. ``ema_cross`` - the FIRST close through EMA9 (long: close < ema,
   short: close > ema).  This is the structural exit.

A bar whose high/low touches the stop *and* the target fills the **stop**
(conservative — the same rule the rest of the engine uses).

VOLUME
------
yfinance forex data has zero/absent volume.  A volume-weighted VWAP is
meaningless without volume, so the strategy **refuses** to run on a symbol
whose volume is absent rather than silently falling back to an unweighted
average.  ``has_real_volume`` implements that check.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "ohlc4",
    "session_mask",
    "trading_period_ids",
    "anchored_vwap",
    "has_real_volume",
    "vwap_prepare",
    "vwap_signals",
]


# ---------------------------------------------------------------------------
# VWAP building blocks
# ---------------------------------------------------------------------------


def ohlc4(df: pd.DataFrame) -> pd.Series:
    """Typical price: (open + high + low + close) / 4."""
    return (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0


def session_mask(index: pd.DatetimeIndex, session_start_utc: int,
                 session_end_utc: int) -> np.ndarray:
    """True for bars whose UTC hour falls in [start, end).

    A tz-naive index is treated as UTC.  The window is half-open so the
    16:00 bar is excluded from a 07:00-16:00 session.
    """
    if not isinstance(index, pd.DatetimeIndex):
        return np.ones(len(index), dtype=bool)
    utc = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    hours = utc.hour.to_numpy()
    return (hours >= session_start_utc) & (hours < session_end_utc)


def trading_period_ids(index: pd.DatetimeIndex, anchor: str) -> np.ndarray:
    """Integer period id per bar: trading-day or trading-week ordinal.

    Weeks use the ISO year+week so week 1 of two different years never
    collide.  ``previous_week`` and ``current_week`` share the week ids; they
    differ only in whether the previous period is carried into the sum (see
    ``anchored_vwap``).
    """
    if not isinstance(index, pd.DatetimeIndex):
        return np.zeros(len(index), dtype=np.int64)
    utc = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    if anchor in ("previous_week", "current_week"):
        iso = utc.isocalendar()
        raw = np.asarray(iso["year"], dtype=np.int64) * 100 + np.asarray(iso["week"], dtype=np.int64)
    else:
        naive = utc.tz_localize(None) if utc.tz is not None else utc
        raw = naive.normalize().to_numpy().astype("datetime64[D]").astype(np.int64)
    # factorize keeps the ids compact and ordered
    _, ids = np.unique(raw, return_inverse=True)
    return ids.astype(np.int64)


def anchored_vwap(df: pd.DataFrame, anchor: str = "previous_day",
                   session_start_utc: int = 7, session_end_utc: int = 16) -> pd.Series:
    """Volume-weighted average price anchored to the previous trading period.

    See the module docstring for the exact definition.  Returns a Series of
    ``NaN`` where the anchor has no volume to work with.
    """
    out = pd.Series(np.nan, index=df.index, dtype=float)
    if df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return out

    in_session = session_mask(df.index, session_start_utc, session_end_utc)
    vol_col = "tick_volume" if "tick_volume" in df.columns else (
        "volume" if "volume" in df.columns else None)
    if vol_col is None:
        return out

    vol = df[vol_col].to_numpy(dtype=float)
    vol = np.where(np.isfinite(vol), vol, 0.0)
    px = ohlc4(df).to_numpy(dtype=float)

    # Only session bars contribute to the sums.
    pv = np.where(in_session, px * vol, 0.0)
    vv = np.where(in_session, vol, 0.0)

    period = trading_period_ids(df.index, anchor)
    n_periods = int(period.max()) + 1 if len(period) else 0

    # Cumulative sum inside the current period.
    cum_pv = np.zeros(len(df), dtype=float)
    cum_v = np.zeros(len(df), dtype=float)
    per_period_pv = np.zeros(n_periods, dtype=float)
    per_period_v = np.zeros(n_periods, dtype=float)
    for p in range(n_periods):
        mask = period == p
        if not mask.any():
            continue
        per_period_pv[p] = pv[mask].sum()
        per_period_v[p] = vv[mask].sum()
        cum_pv[mask] = np.cumsum(pv[mask])
        cum_v[mask] = np.cumsum(vv[mask])

    # The anchor for period p is period p-1, so the running total adds the
    # whole previous period on top of the current period's running sum.
    # ``current_day`` / ``current_week`` anchor *inside* the current period
    # instead (the line resets to zero at each period boundary).
    prev_pv = np.zeros(n_periods, dtype=float)
    prev_v = np.zeros(n_periods, dtype=float)
    if n_periods > 1 and not str(anchor).startswith("current"):
        prev_pv[1:] = per_period_pv[:-1]
        prev_v[1:] = per_period_v[:-1]

    anchor_pv = prev_pv[period] + cum_pv
    anchor_v = prev_v[period] + cum_v
    with np.errstate(invalid="ignore", divide="ignore"):
        vwap = np.where(anchor_v > 0, anchor_pv / anchor_v, np.nan)
    out.iloc[:] = vwap
    return out


# ---------------------------------------------------------------------------
# Volume gate
# ---------------------------------------------------------------------------


def has_real_volume(df: pd.DataFrame, min_volume: float = 1000.0) -> bool:
    """True when the frame carries usable volume data.

    yfinance forex feeds publish zero volume for every bar, which makes a
    volume-weighted VWAP degenerate.  The median is used because a single
    zero-volume bar (settlement) must not disqualify an otherwise real
    series.
    """
    if df.empty:
        return False
    col = "tick_volume" if "tick_volume" in df.columns else (
        "volume" if "volume" in df.columns else None)
    if col is None:
        return False
    vol = pd.to_numeric(df[col], errors="coerce").dropna()
    if vol.empty:
        return False
    return float(vol.median()) > float(min_volume)


# ---------------------------------------------------------------------------
# Strategy plumbing
# ---------------------------------------------------------------------------


def vwap_prepare(df: pd.DataFrame, params) -> pd.DataFrame:
    """Add the columns the vwap_ema_cross signal generator needs."""
    from .indicators import atr, ema

    out = df.copy()
    out["ema"] = ema(out["close"], params.ema_period)
    out["vwap"] = anchored_vwap(
        df,
        params.vwap_anchor,
        params.session_start_utc,
        params.session_end_utc,
    )
    out["atr"] = atr(out, params.atr_period)
    return out


def vwap_signals(df: pd.DataFrame, params) -> pd.Series:
    """+1 / -1 / 0 signal series for the vwap_ema_cross strategy.

    A signal is produced on the **close** of bar ``t``; the engine fills at
    the open of bar ``t+1``.
    """
    if df.empty:
        return pd.Series(dtype=int, index=df.index)

    close = df["close"]
    ema_ = df["ema"]
    vwap = df["vwap"]

    cross_up = (close > ema_) & (close.shift(1) <= ema_.shift(1))
    cross_dn = (close < ema_) & (close.shift(1) >= ema_.shift(1))

    ready = ema_.notna() & vwap.notna()
    long_sig = ready & cross_up & (close > vwap)
    short_sig = ready & cross_dn & (close < vwap)

    sig = pd.Series(0, index=df.index, dtype=int)
    sig[long_sig.fillna(False)] = 1
    sig[short_sig.fillna(False)] = -1
    sig[long_sig.fillna(False) & short_sig.fillna(False)] = 0
    return sig
