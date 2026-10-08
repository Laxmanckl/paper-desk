"""Glue: price ticks -> candles -> signals -> paper trades.

Feed-agnostic: Binance, MT5, a backtest or a test all call `on_tick` and the
engine does the rest. One engine trades every instrument into one account.
"""

from __future__ import annotations

from . import instruments
from .bars import Bar, BarBuilder
from .book import ScalpBook
from .strategy import ScalpConfig, evaluate


class Engine:
    def __init__(self, book: ScalpBook, symbols: list[str], cfg: ScalpConfig | None = None):
        self.book = book
        self.cfg = cfg or ScalpConfig()
        self.book.config = dict(self.cfg.__dict__)
        self.symbols = [instruments.get(s).symbol for s in symbols]
        self.builders = {s: BarBuilder(keep=1000) for s in self.symbols}   # ~16 h: enough for 15-min trends
        self.state: dict = {s: {} for s in self.symbols}                   # per-strategy memory
        self.closed_trades: list = []          # trades closed since the caller last drained this
        self.opened: list = []

    def seed(self, sym: str, bars: list[Bar]) -> None:
        self.builders[sym].seed(bars)

    def on_tick(self, sym: str, bid: float, ask: float, t: float) -> None:
        if sym not in self.builders:
            return
        tr = self.book.on_tick(sym, bid, ask, t)
        if tr:
            self.closed_trades.append(tr)
        done = self.builders[sym].update(t, (bid + ask) / 2)
        if done is not None:
            self.on_bar(sym, t)

    def on_bar(self, sym: str, t: float) -> None:
        bars = self.builders[sym].bars
        self.book.note_spread(sym)
        sig = evaluate(bars, self.cfg, t, self.state[sym])
        info = {"trend": sig.trend, "rsi": round(sig.rsi, 1) if sig.rsi is not None else None,
                "atr": sig.atr, "action": sig.action, "reason": sig.reason, "t": t}
        if sig.action:
            res = self.book.open(sym, sig.action, sig.atr, t, self.cfg,
                                 stop_price=sig.stop, deadline=sig.deadline)
            if isinstance(res, dict):
                self.opened.append(res)
                self.state[sym]["traded"] = int(t // 86_400) * 86_400
                info["reason"] = f"{sig.action}: {sig.reason}"
            else:
                info["reason"] = f"{sig.action} signal skipped: {res}"
        self.book.signals[sym] = info
        self.book.mark(t)

    def drain(self) -> tuple[list, list]:
        """Trades opened and closed since the last call (for alerts)."""
        o, c = self.opened, self.closed_trades
        self.opened, self.closed_trades = [], []
        return o, c


class Fanout:
    """Several engines (strategies, each with its own paper account) on one price feed.
    Looks like a single Engine to the feeds; `book` is the first engine's account,
    which the feeds use for their status and the 1-second clock."""

    def __init__(self, engines: list[Engine]):
        self.engines = engines
        self.book = engines[0].book
        self.builders = engines[0].builders

    def seed(self, sym: str, bars: list[Bar]) -> None:
        for e in self.engines:
            e.seed(sym, [Bar(b.t, b.o, b.h, b.l, b.c) for b in bars])

    def on_tick(self, sym: str, bid: float, ask: float, t: float) -> None:
        for e in self.engines:
            e.on_tick(sym, bid, ask, t)
