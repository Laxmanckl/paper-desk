"""Plain data types shared by every stage. Pure data, no logic.

All timestamps are UTC unix seconds. The Windows bridge converts MT5's broker
time to UTC before sending.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["buy", "sell"]

BAR_SECONDS = 300  # M5 bars


@dataclass(frozen=True)
class Quote:
    symbol: str
    ts: float
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class Bar:
    """One closed M5 bar. ts is the bar's OPEN time; it closes at ts + 300."""
    ts: float
    open: float
    high: float
    low: float
    close: float
    tick_volume: float
    spread_pts: float  # broker spread in points, as MT5 reports it

    @property
    def close_ts(self) -> float:
        return self.ts + BAR_SECONDS


@dataclass(frozen=True)
class Fill:
    ts: float
    side: Side  # our side
    price: float
    size: float
    expected_price: float
