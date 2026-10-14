"""Stage 4: the risk engine (guide Step 5). It always has the final word.

Every check uses numbers the code computes itself (equity, spread, quote age,
positions, the clock), never the model saying things are fine.

- size_lots(): the stop sets the size, so hitting it loses risk_per_trade_pct
- entry_vetoes(): checked before every new position; any veto blocks it
- exit_reason(): checked every few seconds on live quotes (stop, target, max hold, Friday close)
- kill_check(): daily-loss and drawdown kill switches (close everything, stop trading)
"""
from __future__ import annotations

import math
import time

from .instruments import CONTRACT, INSTRUMENTS, pip_value_usd
from .paper import PaperBook, Position, exit_price
from .settings import Settings
from .types import Quote


def size_lots(equity: float, risk_pct: float, stop_pips: float, pip_value: float, max_lots: float) -> float:
    if stop_pips <= 0 or pip_value <= 0:
        return 0.0
    raw = equity * risk_pct / 100 / (stop_pips * pip_value)
    return round(min(max_lots, math.floor(raw * 100) / 100), 2)


def notional_usd(sym: str, lots: float, price: float) -> float:
    """Position size in US dollars: XXX/USD and gold are priced in USD; USD/XXX is already USD."""
    units = CONTRACT[INSTRUMENTS[sym]["kind"]] * lots
    return units * price if sym.endswith("USD") else units


def usd_bet(sym: str, side: str) -> int:
    """+1 if the position wins when the dollar strengthens, -1 if it wins when the dollar weakens."""
    return INSTRUMENTS[sym]["usd_sign"] * (1 if side == "long" else -1)


def _utc_minutes(t: float) -> tuple[int, int]:
    g = time.gmtime(t)
    return g.tm_wday, g.tm_hour * 60 + g.tm_min


class RiskEngine:
    def __init__(self, settings: Settings):
        self.s = settings

    @property
    def r(self) -> dict:
        return self.s.risk

    # ---- sizing ------------------------------------------------------------
    def plan(self, sym: str, side: str, q: Quote, atr_pips: float | None, equity: float,
             notional_open: float = 0.0, risk_mult: float = 1.0) -> dict | None:
        """Stop, target and size for a new position. None if volatility is unknown.
        Size: the stop costs risk_per_trade_pct x risk_mult of equity, then cut down if needed
        so total open size stays within max_leverage x equity."""
        if not atr_pips or atr_pips <= 0:
            return None
        pip = INSTRUMENTS[sym]["pip"]
        digits = INSTRUMENTS[sym]["digits"]
        stop_pips = atr_pips * self.r["stop_atr_mult"]
        entry = q.ask if side == "long" else q.bid
        d = 1 if side == "long" else -1
        stop = round(entry - d * stop_pips * pip, digits)
        target = round(entry + d * stop_pips * self.r["target_r_multiple"] * pip, digits)
        lots = size_lots(equity, self.r["risk_per_trade_pct"] * risk_mult, stop_pips, pip_value_usd(sym, entry), self.r["max_lots"])
        room = max(0.0, self.r["max_leverage"] * equity - notional_open)
        per_lot = notional_usd(sym, 1.0, entry)
        capped = math.floor(room / per_lot * 100) / 100 if per_lot > 0 else 0.0
        lev_cut = capped < lots
        lots = round(min(lots, capped), 2)
        return {"entry": entry, "stop": stop, "target": target, "stop_pips": round(stop_pips, 1), "lots": lots,
                "leverage_cut": lev_cut, "leverage_after": round((notional_open + per_lot * lots) / equity, 2) if equity else None}

    # ---- before every new position -----------------------------------------
    def entry_vetoes(self, sym: str, side: str, snap: dict, q: Quote | None, now: float, book: PaperBook,
                     jev_rec: dict | None, jev_consec_errors: int, plan: dict | None, source: str = "jev") -> list[str]:
        r, v = self.r, []
        if book.halted:
            v.append(f"kill switch on ({book.halted['reason']})")
        if sym in book.positions:
            v.append("already in a position")
        if len(book.positions) >= r["max_open_positions"]:
            v.append(f"max open positions ({r['max_open_positions']}) reached")
        same = sum(1 for s, p in book.positions.items() if usd_bet(s, p.side) == usd_bet(sym, side))
        if same >= r["max_same_usd_direction"]:
            v.append(f"already {same} position(s) betting the same way on the US dollar")
        if q is None or now - q.ts > r["max_quote_age_s"]:
            v.append("price data too old")
        svn = snap.get("spread_vs_norm")
        if svn is not None and svn > r["max_spread_vs_normal"]:
            v.append(f"spread {svn:.1f}x normal (max {r['max_spread_vs_normal']}x)")
        if source == "jev" and jev_consec_errors >= r["max_jev_errors_in_a_row"]:
            v.append(f"Jev failed {jev_consec_errors} rounds in a row")
        if jev_rec and (jev_rec.get("ms") or 0) > r["max_decision_ms"]:
            v.append(f"Jev answer took {jev_rec['ms']} ms (max {r['max_decision_ms']})")
        wday, mins = _utc_minutes(now)
        if wday == 4 and mins >= self.s.minutes("friday_no_entries_utc") or wday in (5, 6):
            v.append("weekend close: no new positions")
        if plan is None:
            v.append("volatility unknown, can't place a stop")
        elif plan["lots"] < 0.01:
            v.append(f"max leverage {r['max_leverage']:g}x reached" if plan.get("leverage_cut")
                     else f"stop of {plan['stop_pips']} pips is too wide for the risk budget at 0.01 lots")
        return v

    # ---- every few seconds on live quotes ----------------------------------
    def exit_reason(self, pos: Position, q: Quote, now: float) -> str | None:
        px = exit_price(pos.side, q)
        if (pos.dir == 1 and px <= pos.stop) or (pos.dir == -1 and px >= pos.stop):
            return "stop" if pos.stop == pos.initial_stop else "trailing stop"
        if (pos.dir == 1 and px >= pos.target) or (pos.dir == -1 and px <= pos.target):
            return "target"
        if now - pos.opened >= self.r["max_hold_hours"] * 3600:
            return f"max hold {self.r['max_hold_hours']:g} h"
        wday, mins = _utc_minutes(now)
        if wday == 4 and mins >= self.s.minutes("friday_flat_utc"):
            return "flat before the weekend"
        return None

    def kill_check(self, m: dict) -> tuple[str, str] | None:
        if m["drawdown_pct"] >= self.r["max_drawdown_pct"]:
            return "drawdown", f"equity {m['drawdown_pct']:.2f}% below peak (max {self.r['max_drawdown_pct']}%)"
        if m["daily_loss_pct"] >= self.r["max_daily_loss_pct"]:
            return "daily", f"down {m['daily_loss_pct']:.2f}% today (max {self.r['max_daily_loss_pct']}%)"
        return None

    # ---- dashboard -----------------------------------------------------------
    def limits_view(self, book: PaperBook, m: dict, jev_consec_errors: int, worst_spread: float | None,
                    oldest_quote_s: float | None, last_jev_ms: float | None) -> list[dict]:
        r = self.r
        same = {}
        for s, p in book.positions.items():
            same[usd_bet(s, p.side)] = same.get(usd_bet(s, p.side), 0) + 1
        max_lots_now = max((p.lots for p in book.positions.values()), default=0)
        risk_now = max((p.risk_usd for p in book.positions.values()), default=0)
        eq = m.get("equity") or book.start_equity
        lev = sum(notional_usd(s, p.lots, p.entry) for s, p in book.positions.items()) / eq if eq else 0
        rows = [
            ("Max leverage (open size ÷ equity)", round(lev, 2), r["max_leverage"], "×"),
            ("Max position size", max_lots_now, r["max_lots"], "lots"),
            ("Max open positions", len(book.positions), r["max_open_positions"], ""),
            ("Max same-way US dollar bets", max(same.values(), default=0), r["max_same_usd_direction"], ""),
            ("Risk per trade (to stop)", round(risk_now / eq * 100, 2) if eq else 0, r["risk_per_trade_pct"], "%"),
            ("Max daily loss", round(m.get("daily_loss_pct", 0), 2), r["max_daily_loss_pct"], "%"),
            ("Max drawdown", round(m.get("drawdown_pct", 0), 2), r["max_drawdown_pct"], "%"),
            ("Max spread to enter", worst_spread, r["max_spread_vs_normal"], "× normal"),
            ("Max age of price data", oldest_quote_s, r["max_quote_age_s"], "s"),
            ("Max Jev errors in a row", jev_consec_errors, r["max_jev_errors_in_a_row"], ""),
            ("Max decision response time", last_jev_ms, r["max_decision_ms"], "ms"),
            ("Max time in a position", round(max((time.time() - p.opened for p in book.positions.values()), default=0) / 3600, 2),
             r["max_hold_hours"], "h"),
        ]
        out = [{"name": n, "value": None if v is None else round(v, 2), "limit": lim, "unit": u,
                "used": None if v is None or not lim else min(1.5, v / lim)} for n, v, lim, u in rows]
        out.append({"name": "No new trades around major news", "value": None, "limit": "needs a news calendar",
                    "unit": "", "used": None})
        return out
