"""Plain data types shared by every stage. Pure data, no logic."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass(frozen=True)
class OrderBook:
    """Top of book at one block. bids sorted high->low, asks low->high."""
    block: int
    ts: float  # unix seconds, block timestamp
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]


@dataclass(frozen=True)
class Trade:
    ts: float
    price: float
    size: float
    aggressor: Side  # side that crossed the spread


@dataclass(frozen=True)
class OwnQuote:
    """One of our resting orders, as the venue adapter reports it."""
    side: Side
    price: float
    size: float
    size_ahead: float  # venue size queued ahead of us at our price


@dataclass(frozen=True)
class Fill:
    ts: float
    side: Side  # our side
    price: float
    size: float
    expected_price: float  # price we intended; slippage = diff


@dataclass
class BlockInput:
    """Everything the venue adapter delivers for one block.

    Every field must describe information known at or before `book.ts`.
    The state engine enforces this and rejects anything stamped later.
    """
    book: OrderBook
    trades: list[Trade] = field(default_factory=list)
    cancels: int = 0  # venue-wide cancel count since previous block
    own_quotes: list[OwnQuote] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    orders_sent: int = 0
    orders_rejected: int = 0
    ref_price: float | None = None  # mid on a reference exchange
    ref_ts: float | None = None
    funding_rate: float | None = None  # per-interval, venue units
    index_price: float | None = None  # for basis
