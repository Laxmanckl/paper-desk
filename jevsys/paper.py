"""The paper book: fake money, real prices.

Fills use the live MT5 quote: buys at the ask, sells at the bid, so the spread
is paid on every trade exactly as with the broker. Stops and targets fill at
the bid/ask seen when they are hit, so a fast market gives slippage. Swap and
rollover charges are not modelled.

Saved to state/paper_book.json after every change, so a restart keeps open
positions and their stops.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field

from .instruments import INSTRUMENTS, pip_value_usd
from .types import Quote

EQUITY_STEP_S = 300
EQUITY_KEEP_S = 7 * 86_400
TRADES_KEEP = 2_000


@dataclass
class Position:
    id: int
    sym: str
    side: str            # "long" | "short"
    lots: float
    entry: float
    stop: float
    target: float
    opened: float
    initial_stop: float
    risk_usd: float
    entry_spread_pips: float
    reason: str
    jev: dict = field(default_factory=dict)       # Jev's answers at entry, for the review
    settings: str = ""

    @property
    def dir(self) -> int:
        return 1 if self.side == "long" else -1


def exit_price(side: str, q: Quote) -> float:
    """What closing a position would get right now: a long sells at the bid, a short buys at the ask."""
    return q.bid if side == "long" else q.ask


def entry_price(side: str, q: Quote) -> float:
    return q.ask if side == "long" else q.bid


def pnl_usd(pos: Position, px: float) -> float:
    pip = INSTRUMENTS[pos.sym]["pip"]
    return (px - pos.entry) * pos.dir / pip * pip_value_usd(pos.sym, px) * pos.lots


def pnl_pips(pos: Position, px: float) -> float:
    return (px - pos.entry) * pos.dir / INSTRUMENTS[pos.sym]["pip"]


def utc_day(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(t))


class PaperBook:
    def __init__(self, path: str = "state/paper_book.json", start_equity: float = 10_000.0, commission_per_lot: float = 0.0):
        self.path = path
        self.lock = threading.RLock()
        self.start_equity = start_equity
        self.commission_per_lot = commission_per_lot
        self.realized = 0.0
        self.fees = 0.0
        self.peak = start_equity
        self.day = ""
        self.day_start = start_equity
        self.positions: dict[str, Position] = {}
        self.trades: list[dict] = []
        self.halted: dict | None = None
        self.equity_pts: list[list[float]] = []
        self.next_id = 1
        self.last_dd = 0.0
        self.load()

    # ---- persistence ----------------------------------------------------
    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return
        self.start_equity = d.get("start_equity", self.start_equity)
        self.realized, self.fees = d.get("realized", 0.0), d.get("fees", 0.0)
        self.peak, self.day, self.day_start = d.get("peak", self.start_equity), d.get("day", ""), d.get("day_start", self.start_equity)
        self.positions = {p["sym"]: Position(**p) for p in d.get("positions", [])}
        self.trades, self.halted = d.get("trades", []), d.get("halted")
        self.equity_pts, self.next_id = d.get("equity_pts", []), d.get("next_id", 1)

    def save(self) -> None:
        with self.lock:
            d = {"start_equity": self.start_equity, "realized": self.realized, "fees": self.fees, "peak": self.peak,
                 "day": self.day, "day_start": self.day_start, "positions": [asdict(p) for p in self.positions.values()],
                 "trades": self.trades[-TRADES_KEEP:], "halted": self.halted, "equity_pts": self.equity_pts,
                 "next_id": self.next_id}
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, separators=(",", ":"))
        os.replace(tmp, self.path)

    # ---- marking ---------------------------------------------------------
    def unrealized(self, quotes: dict[str, Quote]) -> float:
        """Open P&L at current exit prices. A position without a quote counts at its entry (0)."""
        with self.lock:
            return sum(pnl_usd(p, exit_price(p.side, quotes[s])) for s, p in self.positions.items() if s in quotes)

    def equity(self, quotes: dict[str, Quote]) -> float:
        with self.lock:
            return self.start_equity + self.realized + self.unrealized(quotes)

    def mark(self, quotes: dict[str, Quote], t: float, force_point: bool = False) -> dict:
        """Update peak, the trading day and the equity curve. Returns the numbers the risk engine checks."""
        with self.lock:
            eq = self.equity(quotes)
            self.peak = max(self.peak, eq)
            self.last_dd = (self.peak - eq) / self.peak if self.peak > 0 else 0.0
            day = utc_day(t)
            new_day = day != self.day
            if new_day:
                self.day, self.day_start = day, eq
                if self.halted and self.halted.get("kind") == "daily":
                    self.halted = None
            if force_point or not self.equity_pts or t - self.equity_pts[-1][0] >= EQUITY_STEP_S:
                self.equity_pts.append([round(t), round(eq, 2)])
                cut = t - EQUITY_KEEP_S
                while self.equity_pts and self.equity_pts[0][0] < cut:
                    self.equity_pts.pop(0)
            return {"equity": eq, "peak": self.peak, "day_start": self.day_start,
                    "drawdown_pct": (self.peak - eq) / self.peak * 100 if self.peak > 0 else 0.0,
                    "daily_loss_pct": max(0.0, (self.day_start - eq) / self.day_start * 100) if self.day_start > 0 else 0.0,
                    "new_day": new_day}

    # ---- trading -----------------------------------------------------------
    def open(self, sym: str, side: str, lots: float, q: Quote, stop: float, target: float, t: float,
             reason: str, jev: dict | None = None, settings: str = "") -> Position:
        with self.lock:
            if sym in self.positions:
                raise ValueError(f"{sym} already has a position")
            px = entry_price(side, q)
            pip = INSTRUMENTS[sym]["pip"]
            risk = abs(px - stop) / pip * pip_value_usd(sym, px) * lots
            pos = Position(self.next_id, sym, side, lots, px, stop, target, t, stop, round(risk, 2),
                           round((q.ask - q.bid) / pip, 2), reason, jev or {}, settings)
            self.next_id += 1
            fee = self.commission_per_lot * lots
            self.realized -= fee
            self.fees += fee
            self.positions[sym] = pos
        self.save()
        return pos

    def close(self, sym: str, q: Quote, t: float, why: str) -> dict:
        with self.lock:
            pos = self.positions.pop(sym)
            px = exit_price(pos.side, q)
            fee = self.commission_per_lot * pos.lots
            gross = pnl_usd(pos, px)
            net = gross - fee - self.commission_per_lot * pos.lots  # entry fee was booked at open; report round trip
            self.realized += gross - fee
            self.fees += fee
            trade = {"id": pos.id, "sym": sym, "side": pos.side, "lots": pos.lots, "entry": pos.entry, "exit": px,
                     "opened": pos.opened, "closed": t, "held_min": round((t - pos.opened) / 60, 1),
                     "pips": round(pnl_pips(pos, px), 1), "pnl": round(net, 2),
                     "r": round(gross / pos.risk_usd, 2) if pos.risk_usd else None,
                     "spread_pips": pos.entry_spread_pips,
                     "spread_cost": round(pos.entry_spread_pips * pip_value_usd(sym, px) * pos.lots, 2),
                     "fees": round(2 * self.commission_per_lot * pos.lots, 2),
                     "why": why, "reason": pos.reason, "initial_stop": pos.initial_stop, "stop": pos.stop,
                     "target": pos.target, "jev": pos.jev, "settings": pos.settings}
            self.trades.append(trade)
            self.trades = self.trades[-TRADES_KEEP:]
        self.save()
        return trade

    def move_stop(self, sym: str, new_stop: float) -> bool:
        """Only ever tightens: a stop never moves further away."""
        with self.lock:
            pos = self.positions.get(sym)
            if not pos or (pos.dir == 1 and new_stop <= pos.stop) or (pos.dir == -1 and new_stop >= pos.stop):
                return False
            pos.stop = new_stop
        self.save()
        return True

    def halt(self, kind: str, reason: str, t: float) -> None:
        with self.lock:
            self.halted = {"kind": kind, "reason": reason, "since": t}
        self.save()

    def reset_halt(self) -> None:
        with self.lock:
            self.halted = None
        self.save()

    # ---- views -------------------------------------------------------------
    def position_view(self, sym: str, q: Quote | None, t: float) -> tuple[float, float, float]:
        """(signed lots, unrealized pips, minutes held) for the state snapshot."""
        with self.lock:
            p = self.positions.get(sym)
            if not p:
                return 0.0, 0.0, 0.0
            pips = pnl_pips(p, exit_price(p.side, q)) if q else 0.0
            return p.dir * p.lots, pips, (t - p.opened) / 60

    def stats(self) -> dict:
        with self.lock:
            tr = self.trades
            wins = [x for x in tr if x["pnl"] > 0]
            losses = [x for x in tr if x["pnl"] <= 0]
            gw, gl = sum(x["pnl"] for x in wins), sum(x["pnl"] for x in losses)
            return {"n": len(tr), "wins": len(wins), "win_rate": len(wins) / len(tr) if tr else None,
                    "profit_factor": (gw / -gl) if gl < 0 else (None if not wins else float("inf")),
                    "avg_r": sum(x["r"] or 0 for x in tr) / len(tr) if tr else None,
                    "net": round(sum(x["pnl"] for x in tr), 2), "fees": round(self.fees, 2),
                    "avg_hold_min": sum(x["held_min"] for x in tr) / len(tr) if tr else None}
