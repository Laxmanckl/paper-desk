"""Intraday session helpers shared by the breakout / trend strategies.

Every function here is causal: the value at bar ``t`` depends only on bars
``<= t``.  The two rules that make that true:

* a session range for day *D* is only exposed to bars at/after the range's end
  hour (before that the value is ``NaN``), and
* daily context (EMA, close, ATR) is taken from the **previous completed
  daily bar**, never from the day that is still forming.

The daily frame can be supplied explicitly (``data/{symbol}_1d.csv`` from
``scripts/export_mt5_history.py``, which has years of warm-up) or derived from
the intraday bars.  Supplying it is strongly preferred for anything that needs
a 200-period daily EMA.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .indicators import atr, ema

__all__ = [
    "utc_index",
    "hour_array",
    "bars_per_hour",
    "day_ids",
    "session_range",
    "minute_of_day",
    "close_minute_of_day",
    "bar_minutes",
    "session_range_minutes",
    "daily_bars",
    "daily_state",
    "map_prev_daily",
    "atr_series",
    "volume_array",
    "relative_volume",
]


def utc_index(index: pd.Index) -> pd.DatetimeIndex:
    """Index as a UTC DatetimeIndex (naive timestamps are treated as UTC)."""
    if not isinstance(index, pd.DatetimeIndex):
        raise TypeError("expected a DatetimeIndex")
    return index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")


def hour_array(index: pd.Index) -> np.ndarray:
    """UTC hour of every bar (0-23)."""
    return utc_index(index).hour.to_numpy()


def bars_per_hour(index: pd.Index) -> float:
    """Bar density of the series (1.0 for 1h, 4.0 for 15m, 0.25 for 4h).

    The range filters scale an ATR to the length of the range window with the
    square-root rule, and that only works in *bars*: a 7-hour window holds 7
    hourly bars but 28 fifteen-minute bars.  Reading the density off the data
    keeps the same filter meaning on any timeframe.
    """
    if len(index) < 3:
        return 1.0
    diffs = pd.Series(utc_index(index)).diff().dropna()
    median = diffs.median()
    if not isinstance(median, pd.Timedelta) or median <= pd.Timedelta(0):
        return 1.0
    return float(pd.Timedelta(hours=1) / median)


def day_ordinals(index: pd.Index) -> np.ndarray:
    """Calendar-day ordinal (days since epoch) for every bar, UTC.

    Uses ``datetime64[D]`` rather than ``asi8``: pandas 3 returns a
    microsecond-resolution index from ``normalize()``, so dividing ``asi8`` by
    a nanosecond constant silently collapses every day into one bucket.
    """
    utc = utc_index(index)
    if utc.tz is not None:
        utc = utc.tz_localize(None)
    return utc.normalize().to_numpy().astype("datetime64[D]").astype(np.int64)


def day_ids(index: pd.Index) -> np.ndarray:
    """Compact calendar-day ordinal per bar (same day -> same id)."""
    _, ids = np.unique(day_ordinals(index), return_inverse=True)
    return ids.astype(np.int64)


def minute_of_day(index: pd.Index) -> np.ndarray:
    """UTC minute-of-day of every bar's **open** (13:30 -> 810)."""
    utc = utc_index(index)
    return (utc.hour.to_numpy() * 60 + utc.minute.to_numpy()).astype(np.int64)


def bar_minutes(index: pd.Index) -> float:
    """Bar duration in minutes, read off the series (60 for 1h, 15 for 15m)."""
    if len(index) < 3:
        return 60.0
    diffs = pd.Series(utc_index(index)).diff().dropna()
    median = diffs.median()
    if not isinstance(median, pd.Timedelta) or median <= pd.Timedelta(0):
        return 60.0
    return float(median.total_seconds() / 60.0)


def close_minute_of_day(index: pd.Index) -> np.ndarray:
    """UTC minute-of-day of every bar's **close** (the 14:00 bar closes 15:00)."""
    return minute_of_day(index) + int(round(bar_minutes(index)))


def _day_range(
    df: pd.DataFrame,
    in_window: np.ndarray,
    visible: np.ndarray,
    min_bars: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Per-day high/low of the bars flagged in ``in_window``."""
    ids = day_ids(df.index)
    hi = pd.Series(np.where(in_window, df["high"].to_numpy(dtype=float), np.nan))
    lo = pd.Series(np.where(in_window, df["low"].to_numpy(dtype=float), np.nan))
    count = pd.Series(np.where(in_window, 1.0, 0.0))

    by_day = pd.DataFrame({"hi": hi, "lo": lo, "n": count, "day": ids})
    agg = by_day.groupby("day").agg(hi=("hi", "max"), lo=("lo", "min"), n=("n", "sum"))

    day_hi = agg["hi"].reindex(ids).to_numpy(dtype=float)
    day_lo = agg["lo"].reindex(ids).to_numpy(dtype=float)
    day_n = agg["n"].reindex(ids).to_numpy(dtype=float)

    ok = visible & np.isfinite(day_hi) & np.isfinite(day_lo) & (day_n >= min_bars)
    day_hi = np.where(ok, day_hi, np.nan)
    day_lo = np.where(ok, day_lo, np.nan)
    return day_hi, day_lo


def session_range(
    df: pd.DataFrame,
    start_hour: int,
    end_hour: int,
    visible_from_hour: int | None = None,
    min_bars: int = 3,
) -> tuple[np.ndarray, np.ndarray]:
    """Per-day high/low of the ``[start_hour, end_hour)`` UTC window.

    The value is broadcast to every bar of that calendar day but is ``NaN``
    before ``visible_from_hour`` (default: ``end_hour``), so a bar inside the
    window can never read the range it is supposed to break.

    Days with fewer than ``min_bars`` bars in the window (holidays, half
    sessions) get ``NaN`` because a 30-minute "range" is not a range.
    """
    hours = hour_array(df.index)
    in_window = (hours >= start_hour) & (hours < end_hour)
    visible = hours >= (end_hour if visible_from_hour is None else visible_from_hour)
    return _day_range(df, in_window, visible, min_bars)


def session_range_minutes(
    df: pd.DataFrame,
    start_minute: int,
    end_minute: int,
    visible_from_minute: int | None = None,
    min_bars: int = 3,
) -> tuple[np.ndarray, np.ndarray]:
    """``session_range`` with minute resolution (e.g. 13:30-14:30 UTC).

    Membership is decided by each bar's **open** minute, so on 15m data the
    four bars 13:30/13:45/14:00/14:15 form the window.  On hourly data the
    same window would hold no whole bar, which is why the callers fall back to
    a whole-hour window when the series is coarser than the window.
    """
    mod = minute_of_day(df.index)
    in_window = (mod >= start_minute) & (mod < end_minute)
    visible = mod >= (end_minute if visible_from_minute is None
                      else visible_from_minute)
    return _day_range(df, in_window, visible, min_bars)


def daily_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate intraday bars into calendar-day OHLC (UTC)."""
    if df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    ids = day_ids(df.index)
    grouped = df.groupby(ids)
    out = pd.DataFrame({
        "open": grouped["open"].first(),
        "high": grouped["high"].max(),
        "low": grouped["low"].min(),
        "close": grouped["close"].last(),
    })
    days = pd.DatetimeIndex(sorted(set(utc_index(df.index).normalize())))
    out.index = days
    out.index.name = "date"
    if "tick_volume" in df.columns:
        out["tick_volume"] = grouped["tick_volume"].sum()
    elif "volume" in df.columns:
        out["tick_volume"] = grouped["volume"].sum()
    return out.sort_index()


def daily_state(daily: pd.DataFrame, ema_period: int = 50,
                atr_period: int = 14) -> pd.DataFrame:
    """Daily EMA/ATR plus the two trend flags the strategies use."""
    out = daily.copy()
    out["ema"] = ema(out["close"], ema_period)
    out["atr"] = atr(out, atr_period)
    out["above_ema"] = out["close"] > out["ema"]
    out["ema_rising"] = out["ema"] > out["ema"].shift(1)
    return out


def map_prev_daily(index: pd.Index, daily: pd.DataFrame, column: str) -> np.ndarray:
    """Map a daily column onto intraday bars using the **previous** daily bar.

    A bar on 2024-03-05 sees the daily value of 2024-03-04 (completed), never
    the still-forming 2024-03-05 bar.  ``NaN`` before the first daily bar.
    """
    if daily.empty or column not in daily.columns or len(index) == 0:
        return np.full(len(index), np.nan)
    dates = day_ordinals(daily.index)
    bar_days = day_ordinals(index)
    pos = np.searchsorted(dates, bar_days, side="left") - 1
    values = daily[column].to_numpy(dtype=float)
    out = np.full(len(index), np.nan)
    valid = pos >= 0
    out[valid] = values[pos[valid]]
    return out


def atr_series(df: pd.DataFrame, period: int = 14) -> np.ndarray:
    """ATR as a numpy array (NaN where not warmed up)."""
    return atr(df, period).to_numpy(dtype=float)


def volume_array(df: pd.DataFrame) -> np.ndarray | None:
    """Tick volume (MT5) or volume as floats, or ``None`` when the feed has none.

    yfinance forex publishes zeros; a series whose median is not positive is
    treated as having no volume, so volume rules never run on fake numbers.
    """
    col = "tick_volume" if "tick_volume" in df.columns else (
        "volume" if "volume" in df.columns else None)
    if col is None:
        return None
    vol = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=float)
    finite = vol[np.isfinite(vol)]
    if finite.size == 0 or float(np.median(finite)) <= 0:
        return None
    return np.where(np.isfinite(vol), vol, np.nan)


def relative_volume(df: pd.DataFrame, lookback_days: int = 20,
                    min_days: int = 5) -> np.ndarray:
    """Relative volume (RVOL) per bar: this bar's volume divided by the average
    volume of the **same time-of-day slot** over the previous ``lookback_days``
    trading days.

    Comparing a bar with the same slot on earlier days removes the daily
    rhythm (the 07:00 London bar is always busier than the 03:00 bar), so
    ``RVOL 1.5`` means "50% busier than this hour usually is".  Causal: the
    baseline uses earlier days only (``shift(1)`` inside each slot), and the
    bar's own volume is known once the bar has closed.  ``NaN`` until
    ``min_days`` earlier samples exist, or when the feed has no volume.
    """
    vol = volume_array(df)
    out = np.full(len(df), np.nan)
    if vol is None or len(df) == 0:
        return out
    slot = minute_of_day(df.index)
    s = pd.Series(vol)
    base = s.groupby(slot).transform(
        lambda x: x.shift(1).rolling(lookback_days, min_periods=min_days).mean())
    base = base.to_numpy(dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(np.isfinite(base) & (base > 0), vol / base, np.nan)
    return out
