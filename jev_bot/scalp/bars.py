"""Ticks in, 1-minute candles out, plus the indicators the strategy reads.

Candles are built from the MID price ((bid + ask) / 2) so the spread does not
fake a signal; trades are then filled on the real bid or ask.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Bar:
    t: int          # minute start, unix seconds (UTC)
    o: float
    h: float
    l: float
    c: float


class BarBuilder:
    """Feed it (time, price); it returns a finished Bar when a minute closes."""

    def __init__(self, keep: int = 300):
        self.keep = keep
        self.bars: list[Bar] = []
        self.cur: Bar | None = None

    def seed(self, bars: list[Bar]) -> None:
        self.bars = bars[-self.keep:]

    def update(self, t: float, price: float) -> Bar | None:
        minute = int(t // 60) * 60
        done = None
        if self.cur is not None and minute > self.cur.t:
            done = self.cur
            if not self.bars or done.t > self.bars[-1].t:
                self.bars.append(done)
                self.bars = self.bars[-self.keep:]
            self.cur = None
        if self.cur is None:
            self.cur = Bar(minute, price, price, price, price)
        else:
            c = self.cur
            c.h, c.l, c.c = max(c.h, price), min(c.l, price), price
        return done


# --- indicators (computed on closed bars; cheap enough to redo each minute) ----

def ema(values: list[float], n: int) -> list[float]:
    if not values:
        return []
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def atr(bars: list[Bar], n: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs = [max(b.h - b.l, abs(b.h - p.c), abs(b.l - p.c)) for p, b in zip(bars[-n - 1:-1], bars[-n:])]
    return sum(trs) / len(trs) if trs else 0.0


def rsi(closes: list[float], n: int = 7) -> list[float]:
    """Wilder RSI series (same length as closes; first values are 50)."""
    out = [50.0] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0); losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n, len(closes)):
        if i > n:
            d = closes[i] - closes[i - 1]
            ag = (ag * (n - 1) + max(d, 0)) / n
            al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out
