"""Our own book: position, PnL, drawdown, holding time. Pure arithmetic."""
from __future__ import annotations

from .types import Fill


class AccountState:
    def __init__(self, starting_equity: float):
        self.starting_equity = starting_equity
        self.position = 0.0
        self.avg_entry = 0.0
        self.realized_pnl = 0.0
        self.peak_equity = starting_equity
        self.position_opened_ts: float | None = None

    def apply_fill(self, f: Fill) -> None:
        signed = f.size if f.side == "buy" else -f.size
        pos = self.position
        if pos == 0 or (pos > 0) == (signed > 0):
            # opening or adding
            new_pos = pos + signed
            self.avg_entry = (self.avg_entry * abs(pos) + f.price * abs(signed)) / abs(new_pos)
            if pos == 0:
                self.position_opened_ts = f.ts
            self.position = new_pos
            return
        # reducing, closing or flipping
        closing = min(abs(signed), abs(pos))
        direction = 1 if pos > 0 else -1
        self.realized_pnl += closing * (f.price - self.avg_entry) * direction
        remainder = abs(signed) - closing
        if remainder == 0:
            self.position = pos + signed
            if abs(self.position) < 1e-12:
                self.position, self.avg_entry, self.position_opened_ts = 0.0, 0.0, None
        else:  # flipped through zero
            self.position = remainder * (1 if signed > 0 else -1)
            self.avg_entry = f.price
            self.position_opened_ts = f.ts

    def unrealized(self, mark: float) -> float:
        return self.position * (mark - self.avg_entry) if self.position else 0.0

    def equity(self, mark: float) -> float:
        return self.starting_equity + self.realized_pnl + self.unrealized(mark)

    def mark(self, mark: float) -> float:
        """Update peak and return current drawdown as a fraction (0..1)."""
        eq = self.equity(mark)
        self.peak_equity = max(self.peak_equity, eq)
        return 0.0 if self.peak_equity <= 0 else max(0.0, (self.peak_equity - eq) / self.peak_equity)

    def held_seconds(self, now: float) -> float:
        return 0.0 if self.position_opened_ts is None else max(0.0, now - self.position_opened_ts)
