"""The scalper's paper account. Fills on the real bid/ask, charges fees,
enforces the risk limits, and keeps a bounded history so the file stays small
even after thousands of trades.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from . import instruments
from .strategy import ScalpConfig

MAX_TRADES = 500          # most recent trades kept in full
MAX_EVENTS = 200
MAX_EQUITY = 4320         # 3 days of 1-minute points


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def _day(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


@dataclass
class ScalpBook:
    start_equity: float = 10_000.0
    cash: float = 10_000.0
    created: str = ""
    positions: dict = field(default_factory=dict)      # symbol -> position dict
    trades: list = field(default_factory=list)         # last MAX_TRADES closed trades
    stats: dict = field(default_factory=dict)          # totals over ALL trades, per symbol too
    equity_log: list = field(default_factory=list)     # [iso, equity] per minute
    events: list = field(default_factory=list)         # [iso, text]
    day_start: dict = field(default_factory=dict)      # utc day -> equity at its start
    halted_day: str = ""                               # day trading was halted (loss limit)
    cooldown: dict = field(default_factory=dict)       # symbol -> unix time it may trade again
    today_count: dict = field(default_factory=dict)    # "day|symbol" -> trades opened
    prices: dict = field(default_factory=dict)         # symbol -> {bid, ask, t}
    signals: dict = field(default_factory=dict)        # symbol -> last signal info (dashboard)
    feeds: dict = field(default_factory=dict)          # source -> {status, t}
    spreads: dict = field(default_factory=dict)        # symbol -> spread at each of the last 60 candle closes
    last_tick: float = 0.0
    config: dict = field(default_factory=dict)

    # --- persistence --------------------------------------------------------
    @classmethod
    def load(cls, path: str, start: float = 10_000.0) -> "ScalpBook":
        if os.path.exists(path):
            with open(path) as fh:
                d = json.load(fh)
            return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return cls(start_equity=start, cash=start, created=now)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path + ".tmp", "w") as fh:
            json.dump(asdict(self), fh, separators=(",", ":"))
        os.replace(path + ".tmp", path)

    # --- prices & money -----------------------------------------------------
    def mids(self) -> dict:
        return {s: (p["bid"] + p["ask"]) / 2 for s, p in self.prices.items()}

    def _usd(self, spec, amount_quote: float) -> float:
        conv = instruments.quote_to_usd(spec, self.mids())
        return amount_quote * (conv if conv else 1.0)

    def unrealised(self, sym: str) -> float:
        p, px = self.positions.get(sym), self.prices.get(sym)
        if not p or not px:
            return 0.0
        spec = instruments.get(sym)
        exit_px = px["bid"] if p["side"] == "BUY" else px["ask"]
        d = 1 if p["side"] == "BUY" else -1
        return self._usd(spec, (exit_px - p["entry"]) * d * p["units"])

    def equity(self) -> float:
        return round(self.cash + sum(self.unrealised(s) for s in self.positions), 2)

    def log(self, t: float, text: str) -> None:
        self.events.append([_iso(t), text])
        self.events = self.events[-MAX_EVENTS:]

    # --- the per-tick path --------------------------------------------------
    def on_tick(self, sym: str, bid: float, ask: float, t: float) -> dict | None:
        """Record a price; close the position if its stop/target/time is hit.
        Returns the closed trade, if any."""
        if bid <= 0 or ask <= 0 or ask < bid:
            return None
        self.prices[sym] = {"bid": bid, "ask": ask, "t": t}
        self.last_tick = max(self.last_tick, t)
        day = _day(t)
        if day not in self.day_start:
            self.day_start = {day: self.equity()}          # keep just today
        p = self.positions.get(sym)
        if not p:
            return None
        cfg = ScalpConfig.from_dict(self.config)
        if p["side"] == "BUY":
            if bid <= p["stop"]:
                return self.close(sym, bid, t, "stop")
            if bid >= p["target"]:
                return self.close(sym, p["target"], t, "target")
        else:
            if ask >= p["stop"]:
                return self.close(sym, ask, t, "stop")
            if ask <= p["target"]:
                return self.close(sym, p["target"], t, "target")
        if t >= (p.get("t_deadline") or p["t_open"] + cfg.max_minutes * 60):
            return self.close(sym, bid if p["side"] == "BUY" else ask, t, "time")
        return None

    # --- spread guard -------------------------------------------------------
    def note_spread(self, sym: str) -> None:
        """Called once per closed candle: remember the spread at that moment."""
        px = self.prices.get(sym)
        if px:
            self.spreads[sym] = (self.spreads.get(sym, []) + [px["ask"] - px["bid"]])[-60:]

    def normal_spread(self, sym: str) -> float | None:
        s = sorted(self.spreads.get(sym, []))
        return s[len(s) // 2] if len(s) >= 10 else None

    # --- opening ------------------------------------------------------------
    def can_open(self, sym: str, t: float, cfg: ScalpConfig) -> str | None:
        """None if a new trade is allowed, else the reason it is not."""
        day = _day(t)
        if self.halted_day == day:
            return f"halted for today: daily loss limit ({cfg.daily_loss_halt:.0%}) reached"
        start = self.day_start.get(day, self.equity())
        if self.equity() <= start * (1 - cfg.daily_loss_halt):
            self.halted_day = day
            self.log(t, f"RISK: down {cfg.daily_loss_halt:.0%} today, no new trades until 00:00 UTC")
            return "halted for today: daily loss limit reached"
        if sym in self.positions:
            return "already in a trade"
        if len(self.positions) >= cfg.max_open:
            return f"{cfg.max_open} trades already open"
        if self.cooldown.get(sym, 0) > t:
            return "cooling down after the last trade"
        if self.today_count.get(f"{day}|{sym}", 0) >= cfg.max_trades_per_day:
            return f"{cfg.max_trades_per_day} trades today already"
        spec = instruments.get(sym)
        if spec.kind != "crypto":
            d = datetime.fromtimestamp(t, timezone.utc)
            h = d.hour + d.minute / 60
            if cfg.fx_pause_utc:
                lo, hi = cfg.fx_pause_utc
                if lo <= h < hi:
                    hm = lambda x: f"{int(x):02d}:{round(x % 1 * 60):02d}"
                    return f"daily rollover pause ({hm(lo)}-{hm(hi)} UTC): spreads jump"
            if spec.kind == "metal" and cfg.metal_pause_utc:
                lo, hi = cfg.metal_pause_utc
                if (lo <= h < hi) if lo < hi else (h >= lo or h < hi):
                    return f"no gold trades {int(lo):02d}:00-{int(hi):02d}:00 UTC (thin Asian session)"
        return None

    def open(self, sym: str, side: str, atr_: float, t: float, cfg: ScalpConfig,
             stop_price: float | None = None, deadline: float | None = None) -> dict | str:
        """Open at the live price. Returns the position, or the reason it was skipped."""
        why = self.can_open(sym, t, cfg)
        if why:
            return why
        px = self.prices.get(sym)
        if not px:
            return "no price yet"
        spec = instruments.get(sym)
        if atr_ <= 0 and stop_price is None:
            return "no volatility reading"
        conv = instruments.quote_to_usd(spec, self.mids())
        if conv is None:
            return "missing conversion rate to USD"
        spread = px["ask"] - px["bid"]
        normal = self.normal_spread(sym)
        if cfg.spread_guard and normal and spread > cfg.spread_guard * normal:
            return f"spread {spread / normal:.1f}x its normal level; waiting for it to settle"
        entry = px["ask"] if side == "BUY" else px["bid"]
        # round-trip cost in price terms: the spread plus the fee on both sides
        cost = spread + 2 * self._fee_per_unit(spec, entry, conv, cfg)
        if stop_price is not None:                       # the strategy set its own stop
            stop_dist = (entry - stop_price) if side == "BUY" else (stop_price - entry)
            if stop_dist <= 0:
                return "price already beyond the stop"
            if cost > cfg.max_cost_frac * stop_dist:
                return f"costs too high for this stop ({cost / stop_dist:.0%} of the risk)"
        else:
            stop_dist = max(cfg.stop_atr * atr_, cost / cfg.max_cost_frac)
            if stop_dist > cfg.max_stop_atr * atr_:
                return (f"costs too high right now ({cost / atr_:.1f} x the 1-min range); "
                        f"waiting for more movement")
        eq = self.equity()
        units = eq * cfg.risk_frac / (stop_dist * conv)
        units = min(units, eq * spec.max_leverage / (entry * conv))
        fee = self._fee_per_unit(spec, entry, conv, cfg) * units * conv
        d = 1 if side == "BUY" else -1
        pos = {"symbol": sym, "side": side, "entry": entry, "units": units,
               "stop": entry - d * stop_dist, "target": entry + d * cfg.reward_risk * stop_dist,
               "t_open": t, "opened": _iso(t), "fees": fee, "t_deadline": deadline,
               "spread_at_entry": px["ask"] - px["bid"], "cost_frac": round(cost / stop_dist, 3)}
        self.cash -= fee
        self.positions[sym] = pos
        key = f"{_day(t)}|{sym}"
        self.today_count = {k: v for k, v in self.today_count.items() if k.startswith(_day(t))}
        self.today_count[key] = self.today_count.get(key, 0) + 1
        self.log(t, f"{sym}: OPENED {side} at {entry:.{spec.digits}f} "
                    f"(stop {pos['stop']:.{spec.digits}f}, target {pos['target']:.{spec.digits}f})")
        return pos

    @staticmethod
    def _fee_per_unit(spec, price: float, conv: float, cfg: ScalpConfig) -> float:
        """Fee for one unit on one side, in the instrument's QUOTE currency."""
        if spec.kind == "crypto":
            return price * cfg.crypto_fee
        return cfg.fx_commission_per_100k / 100_000 / conv

    # --- closing ------------------------------------------------------------
    def close(self, sym: str, price: float, t: float, reason: str) -> dict:
        p = self.positions.pop(sym)
        spec = instruments.get(sym)
        conv = instruments.quote_to_usd(spec, self.mids()) or 1.0
        d = 1 if p["side"] == "BUY" else -1
        cfg = ScalpConfig.from_dict(self.config)
        gross = (price - p["entry"]) * d * p["units"] * conv
        fee = self._fee_per_unit(spec, price, conv, cfg) * p["units"] * conv
        fees = p["fees"] + fee
        net = gross - fees
        self.cash += gross - fee
        self.cooldown[sym] = t + cfg.cooldown_min * 60
        risk = abs(p["entry"] - p["stop"]) * p["units"] * conv
        tr = {"symbol": sym, "side": p["side"], "entry": p["entry"], "exit": price,
              "opened": p["opened"], "closed": _iso(t), "secs": round(t - p["t_open"]),
              "reason": reason, "gross": round(gross, 2), "fees": round(fees, 2),
              "pnl": round(net, 2), "r": round(net / risk, 2) if risk else 0.0,
              "pips": round((price - p["entry"]) * d / spec.pip, 1) if spec.kind != "crypto" else None,
              "move_pct": round((price / p["entry"] - 1) * d * 100, 3)}
        self.trades.append(tr)
        self.trades = self.trades[-MAX_TRADES:]
        for key in ("_all", sym):
            s = self.stats.setdefault(key, {"n": 0, "wins": 0, "gross_win": 0.0, "gross_loss": 0.0,
                                            "fees": 0.0, "pnl": 0.0, "secs": 0})
            s["n"] += 1
            s["wins"] += net > 0
            s["gross_win" if net > 0 else "gross_loss"] += net
            s["fees"] += fees
            s["pnl"] += net
            s["secs"] += tr["secs"]
        move = f"{tr['pips']:+.1f} pips" if tr["pips"] is not None else f"{tr['move_pct']:+.2f}%"
        self.log(t, f"{sym}: CLOSED {p['side']} ({reason}) {move}, "
                    f"net ${net:+,.2f} after ${fees:,.2f} costs")
        return tr

    def mark(self, t: float) -> None:
        """One equity point per minute for the chart."""
        minute = _iso(int(t // 60) * 60)
        if not self.equity_log or self.equity_log[-1][0] != minute:
            self.equity_log.append([minute, self.equity()])
            self.equity_log = self.equity_log[-MAX_EQUITY:]
        else:
            self.equity_log[-1][1] = self.equity()
