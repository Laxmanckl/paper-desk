"""Technical indicators — pure pandas/numpy, no TA-Lib dependency.

All functions take/return pd.Series or pd.DataFrame aligned to the input index.
No function uses future data: every output at index t depends only on <= t.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(close: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return close.ewm(span=period, adjust=False, min_periods=period).mean()


def wilder_rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's running moving average (used by RSI/ATR)."""
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder smoothing)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = wilder_rma(gain, period)
    avg_loss = wilder_rma(loss, period)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    # When avg_loss == 0 and avg_gain > 0 -> RSI 100; both 0 -> 50 (flat market)
    out = out.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    out = out.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)
    return out


def true_range(df: pd.DataFrame) -> pd.Series:
    """True range from a DataFrame with high/low/close columns."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder smoothing)."""
    return wilder_rma(true_range(df), period)


def bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0
              ) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger bands: (mid, upper, lower)."""
    mid = close.rolling(period, min_periods=period).mean()
    std = close.rolling(period, min_periods=period).std(ddof=0)
    return mid, mid + num_std * std, mid - num_std * std
