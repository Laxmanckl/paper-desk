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
        self.builders = {s: BarBuilder() for s in self.symbols}
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
        sig = evaluate(bars, self.cfg)
        info = {"trend": sig.trend, "rsi": round(sig.rsi, 1), "atr": sig.atr,
                "action": sig.action, "reason": sig.reason, "t": t}
        if sig.action:
            res = self.book.open(sym, sig.action, sig.atr, t, self.cfg)
            if isinstance(res, dict):
                self.opened.append(res)
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
