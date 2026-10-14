"""Deterministic simulated order-book feed for tests and dry runs.

Random-walk mid with regime switches, a 3+ level book, random taker trades
and cancels, 300ms blocks. Not a market model; it exists only to exercise
the pipeline with realistic shapes and no network.
"""
from __future__ import annotations

import itertools
import random
from collections.abc import Iterator

from ..types import BlockInput, Level, OrderBook, Trade
from .base import Feed


class SimFeed(Feed):
    name = "sim"
    label = "Simulated order book · 300 ms blocks"

    def __init__(self, n_blocks: int | None = 1000, seed: int = 7, start_ts: float = 1_760_000_000.0,
                 block_s: float = 0.3, mid: float = 3.4127, tick: float = 0.0001, levels: int = 5):
        self.n, self.rng, self.ts, self.dt = n_blocks, random.Random(seed), start_ts, block_s
        self.mid, self.tick, self.levels = mid, tick, levels

    def blocks(self) -> Iterator[BlockInput]:
        rng = self.rng
        drift, vol = 0.0, 0.00015
        for b in (itertools.count() if self.n is None else range(self.n)):
            if rng.random() < 0.01:  # occasional regime change
                drift = rng.choice([0.0, 0.00004, -0.00004])
                vol = rng.choice([0.0001, 0.00015, 0.0004])
            self.mid *= 1 + drift + rng.gauss(0, vol)
            half = self.tick * rng.randint(1, 3)
            bid0 = round((self.mid - half) / self.tick) * self.tick
            ask0 = max(bid0 + self.tick, round((self.mid + half) / self.tick) * self.tick)
            bids = tuple(Level(round(bid0 - i * self.tick, 6), round(rng.uniform(50, 800), 2)) for i in range(self.levels))
            asks = tuple(Level(round(ask0 + i * self.tick, 6), round(rng.uniform(50, 800), 2)) for i in range(self.levels))
            ts = self.ts + b * self.dt
            trades = [
                Trade(ts - rng.uniform(0, self.dt), ask0 if side == "buy" else bid0, round(rng.uniform(1, 120), 2), side)
                for side in (rng.choice(["buy", "sell"]) for _ in range(rng.randint(0, 4)))
            ]
            trades.sort(key=lambda t: t.ts)
            yield BlockInput(
                book=OrderBook(block=b + 1, ts=ts, bids=bids, asks=asks),
                trades=trades,
                cancels=rng.randint(0, 6),
                ref_price=self.mid * (1 + rng.gauss(0, 0.0001)),
                ref_ts=ts - 0.1,
                funding_rate=0.0001,
                index_price=self.mid * (1 + rng.gauss(0, 0.00005)),
            )
