"""Editable settings. Later stages add policy thresholds and risk limits here."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StateConfig:
    max_snapshot_tokens: int = 400   # guide Step 3 budget
    decide_every_min: int = 15       # one decision per symbol every N minutes (5, 10, 15...)
    grace_s: float = 20.0            # wait after the bar closes for the bridge to deliver it
    max_quote_age_s: float = 120.0   # older quote = stale, no decision (guide: never act on stale data)
    keep_bars: int = 2_100           # ~7 days of M5 bars per symbol
    atr_period: int = 14
    rsi_period: int = 14
    ema_fast: int = 20
    ema_slow: int = 100
