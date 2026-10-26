"""Runs volume breakouts on live hourly bars and trades their paper accounts.

Each time the MT5 bridge delivers a newly closed hourly bar for an instrument,
the validated strategy code (``lib/strategy_volume_breakout.py``) is run on that
instrument's hourly history. If it publishes an entry plan on the bar that just
closed, the position is opened at the live bid/ask, which is the next bar's open,
exactly as in the backtest.

Three sessions, each with its own paper account and kill switch:
  * london  (settings.vbo)      - Asian range (00:00-07:00 UTC) → entry 07:00-11:00 UTC.
                                  The only session validated on real MT5 data.
  * ny      (settings.vbo_ny)   - London range (07:00-13:00 UTC) → entry 13:00-18:00 UTC.
  * asia    (settings.vbo_asia) - NY range (13:00-20:00 UTC) → entry 20:00-23:00 UTC.
                                  (Fills into the Sydney / Tokyo open overnight.)

The engine rules are the same as the Trading-Bot backtester:
  * entries only when the fill hour is inside this session's entry window
  * one trade per instrument per UTC day per session
  * no entry within ``news_window_min`` of a listed release for either currency
  * at most ``max_open_positions`` open; risk ``risk_per_trade_pct`` of equity to the stop
  * stop at least max(min stop distance, spread) from the fill, otherwise skip
  * after a 2% losing day or 4 losses in a row, no new entries until the next UTC day
  * stop / target, and a forced close at ``exit_utc_hour`` UTC
Paper only: no order ever reaches the broker.
"""
from __future__ import annotations

import math
import os
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd

from ..instruments import INSTRUMENTS, pip_value_usd
from ..paper import exit_price
from ..risk import RiskEngine, notional_usd, size_lots
from .lib.news import load_news_filter, symbol_currencies
from .lib.strategy_volume_breakout import build_plans

STRATEGY = "volume_breakout"
MIN_H1_BARS = 2_000          # ~4 months: daily EMA50, 20-day volume and ATR averages are settled
FRESH_S = 15 * 60            # a bar older than this when it arrives is history, not a signal
MIN_SL = {"XAUUSD": 0.50}    # Trading-Bot config.yaml min_sl_distance_price
MIN_SL_DEFAULT = 0.00020
NEWS_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "config", "news_blackout.csv")

SESSION_LABELS = {"london": "London breakout", "ny": "NY breakout", "asia": "Asia breakout"}


def strategy_params(v: dict) -> SimpleNamespace:
    """StrategyParams.volume_breakout() with this session's windows and tuned values."""
    return SimpleNamespace(
        name=STRATEGY, timeframe="1h", atr_period=14,
        range_start_utc=int(v["range_start_utc"]), range_end_utc=int(v["range_end_utc"]),
        entry_start_utc=int(v["entry_start_utc"]), entry_end_utc=int(v["entry_end_utc"]),
        daily_ema_period=50, require_daily_trend=True,
        range_atr_horizon=int(v["range_end_utc"]) - int(v["range_start_utc"]),
        range_atr_min=1.0, range_atr_max=2.5, sl_cap_atr=float(v["sl_cap_atr"]), tp_r=float(v["tp_r"]),
        min_rvol=float(v["min_rvol"]), rvol_lookback_days=20, require_vwap_side=True,
        vwap_anchor="previous_day", atr_regime_min=1.0, atr_regime_days=20,
    )


def bars_frame(bars) -> pd.DataFrame:
    """Hourly bars as the strategy expects them: UTC (tz-naive) open-time index, OHLC + tick_volume."""
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "tick_volume"])
    idx = pd.to_datetime([b.ts for b in bars], unit="s")
    return pd.DataFrame({"open": [b.open for b in bars], "high": [b.high for b in bars],
                         "low": [b.low for b in bars], "close": [b.close for b in bars],
                         "tick_volume": [b.tick_volume for b in bars]}, index=idx)


def _utc_day(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(t))


class VboRisk(RiskEngine):
    """A VBO session's own limits and exit rules. Reads its settings section at every call."""

    def __init__(self, settings, session_name: str = "london"):
        super().__init__(settings)
        self.session_name = session_name

    @property
    def r(self) -> dict:
        return self.s.vbo_session(self.session_name)

    def exit_reason(self, pos, q, now: float) -> str | None:
        px = exit_price(pos.side, q)
        if (pos.dir == 1 and px <= pos.stop) or (pos.dir == -1 and px >= pos.stop):
            return "stop"
        if (pos.dir == 1 and px >= pos.target) or (pos.dir == -1 and px <= pos.target):
            return "target"
        g = time.gmtime(now)
        if _utc_day(now) != _utc_day(pos.opened) or g.tm_hour >= int(self.r["exit_utc_hour"]):
            return f"{int(self.r['exit_utc_hour']):02d}:00 UTC close"
        if g.tm_wday == 4 and g.tm_hour * 60 + g.tm_min >= self.s.minutes("friday_flat_utc"):
            return "flat before the weekend"
        return None

    def kill_check(self, m: dict) -> tuple[str, str] | None:
        if m["drawdown_pct"] >= self.r["max_drawdown_pct"]:
            return "drawdown", f"equity {m['drawdown_pct']:.2f}% below peak (max {self.r['max_drawdown_pct']}%)"
        if m["daily_loss_pct"] >= self.r["max_daily_loss_pct"]:
            return "daily", f"down {m['daily_loss_pct']:.2f}% today (max {self.r['max_daily_loss_pct']}%)"
        return None


class VolumeBreakout:
    def __init__(self, market, trader, settings, session_name: str = "london",
                 news_file: str = NEWS_FILE):
        self.market, self.trader, self.settings = market, trader, settings
        self.session_name = session_name
        self.label = SESSION_LABELS.get(session_name, session_name)
        self.news = load_news_filter(news_file, int(self.cfg["news_window_min"]))
        self.done: dict[str, tuple] = {}       # (newest bar open time, bar count) last looked at, per symbol
        self.latest: dict[str, dict] = {}      # what the dashboard shows per instrument
        self.evaluations = 0

    @property
    def cfg(self) -> dict:
        return self.settings.vbo_session(self.session_name)

    @property
    def book(self):
        return self.trader.book

    @property
    def fill_window(self) -> tuple[int, int]:
        """UTC hour window during which an entry fill is allowed."""
        v = self.cfg
        return int(v["entry_start_utc"]), int(v["entry_end_utc"]) + 1  # +1 so a signal on bar [end-1] can fill

    # ---- called every 2 s by the desk monitor (under the trade lock) ------------
    def tick(self, now: float, quotes: dict) -> None:
        v = self.cfg
        series = {}
        with self.market.lock:   # copy a symbol's history only when it has a new closed bar
            for s, sd in self.market.data.items():
                n = sd.h1.count_closed(now)
                if n == 0:
                    self.latest.setdefault(s, {"status": "waiting", "bars": 0,
                                               "why": "no hourly bars yet (needs MT5 bridge 1.2)"})
                elif self.done.get(s) != (sd.h1.bars[n - 1].ts, n):  # a new bar, or history arrived
                    series[s] = sd.h1.bars[:n]
        for sym, bars in series.items():
            last = bars[-1]
            self.done[sym] = (last.ts, len(bars))
            if len(bars) < MIN_H1_BARS:
                self.latest[sym] = {"status": "waiting", "bars": len(bars), "t": last.ts + 3600,
                                    "why": f"loading hourly history ({len(bars)} of {MIN_H1_BARS} bars)"}
                continue
            if now - (last.ts + 3600) > FRESH_S:
                self.latest[sym] = {"status": "idle", "bars": len(bars), "t": last.ts + 3600,
                                    "why": "latest hourly bar is old (market closed or bridge catching up)"}
                continue
            if not v["enabled"]:
                self.latest[sym] = {"status": "off", "bars": len(bars), "t": last.ts + 3600,
                                    "why": f"{self.label} switched off in config/desk.json"}
                continue
            self.latest[sym] = self.evaluate(sym, bars, now, quotes.get(sym))

    # ---- one closed bar ----------------------------------------------------------
    def evaluate(self, sym: str, bars, now: float, q) -> dict:
        self.evaluations += 1
        v = self.cfg
        df = bars_frame(bars)
        plans, meta = build_plans(df, strategy_params(v), None, explain=True)
        plan = plans[-1]
        rvol = meta.get("rvol")
        rec = {"bars": len(bars), "t": bars[-1].ts + 3600,
               "rvol": None if rvol is None or not np.isfinite(rvol[-1]) else round(float(rvol[-1]), 2)}
        if plan is None:
            why = str(meta["reasons"][-1]).replace("no entry - ", "")
            # The strategy library hard-codes "London entry window" for every session;
            # substitute this session's label so the dashboard reads correctly.
            if self.session_name != "london":
                why = why.replace("London entry window", f"{self.label} entry window")
                why = why.replace("Asian range", f"{self.label} range")
            return {**rec, "status": "stand aside", "why": why}
        side = "long" if plan.direction > 0 else "short"
        vetoes = self.vetoes(sym, side, plan, now, q)
        if vetoes:
            why = "; ".join(vetoes)
            self.trader.event(f"{sym} {side} breakout skipped: {why}")
            return {**rec, "status": "skipped", "side": side, "why": why}
        pos = self.open(sym, side, plan, now, q)
        if pos is None:
            return {**rec, "status": "skipped", "side": side, "why": "position size below 0.01 lots"}
        return {**rec, "status": "entered", "side": side, "why": pos.reason, "position_id": pos.id}

    def vetoes(self, sym: str, side: str, plan, now: float, q) -> list[str]:
        v, book, out = self.cfg, self.book, []
        today = _utc_day(now)
        hour = time.gmtime(now).tm_hour
        w0, w1 = self.fill_window
        if book.halted:
            out.append(f"kill switch on ({book.halted['reason']})")
        if not w0 <= hour < w1:
            out.append(f"fill hour {hour:02d}:00 outside {w0:02d}:00-{w1:02d}:00 UTC")
        if sym in book.positions:
            out.append("already in a position")
        elif any(t["sym"] == sym and _utc_day(t["opened"]) == today for t in book.trades):
            out.append("already traded this instrument today")
        if len(book.positions) >= v["max_open_positions"]:
            out.append(f"max open positions ({v['max_open_positions']}) reached")
        blocked, why = self.news.blocked(pd.Timestamp(now, unit="s"), symbol_currencies(sym))
        if blocked:
            out.append(why)
        streak = 0
        for t in reversed(book.trades):
            if _utc_day(t["closed"]) != today or t["pnl"] > 0:
                break
            if t["pnl"] < 0:
                streak += 1
        if streak >= v["max_consecutive_losses"]:
            out.append(f"{streak} losses in a row today: paused until 00:00 UTC")
        if q is None or now - q.ts > v["max_quote_age_s"]:
            out.append("price data too old")
        else:
            spread = q.ask - q.bid
            if plan.sl_dist < max(MIN_SL.get(sym, MIN_SL_DEFAULT), spread):
                out.append("stop closer than the minimum distance / spread")
        wday = time.gmtime(now).tm_wday
        if wday == 4 and hour * 60 >= self.settings.minutes("friday_no_entries_utc") or wday in (5, 6):
            out.append("weekend close: no new positions")
        return out

    def open(self, sym: str, side: str, plan, now: float, q):
        v, spec = self.cfg, INSTRUMENTS[sym]
        d = 1 if side == "long" else -1
        entry = q.ask if d == 1 else q.bid
        stop = round(entry - d * plan.sl_dist, spec["digits"])
        target = round(entry + d * plan.tp_dist, spec["digits"])
        stop_pips = abs(entry - stop) / spec["pip"]
        equity = self.trader.last_mark.get("equity") or self.book.start_equity
        lots = size_lots(equity, v["risk_per_trade_pct"], stop_pips, pip_value_usd(sym, entry), v["max_lots"])
        room = max(0.0, v["max_leverage"] * equity - sum(notional_usd(s, p.lots, p.entry)
                                                          for s, p in self.book.positions.items()))
        per_lot = notional_usd(sym, 1.0, entry)
        lots = round(min(lots, math.floor(room / per_lot * 100) / 100 if per_lot > 0 else 0.0), 2)
        if lots < 0.01:
            self.trader.event(f"{sym} {side} breakout skipped: position size below 0.01 lots")
            return None
        rv = plan.note.split("rvol=")[-1] if "rvol=" in plan.note else "?"
        why = (f"{self.label} {side} · volume {float(rv):.1f}x normal" if rv != "?"
               else f"{self.label} {side}")
        p = self.book.open(sym, side, lots, q, stop, target, now, why, settings=self.settings.version)
        msg = (f"OPEN {sym} {side} {p.lots:.2f} lots @ {p.entry} · stop {p.stop} · target {p.target} "
               f"· risk ${p.risk_usd:,.2f} · {why}")
        self.trader.event(msg)
        if self.settings.alerts.get("trades"):
            self.trader.alert(f"📈 Paper {msg}")
        return p

    # ---- dashboard ----------------------------------------------------------------
    def view(self) -> dict:
        v = self.cfg
        return {"strategy": STRATEGY, "session": self.session_name, "label": self.label,
                "enabled": v["enabled"], "evaluations": self.evaluations,
                "params": {k: v[k] for k in ("tp_r", "min_rvol", "sl_cap_atr", "risk_per_trade_pct",
                                             "max_open_positions", "exit_utc_hour",
                                             "range_start_utc", "range_end_utc",
                                             "entry_start_utc", "entry_end_utc")},
                "news_events": len(self.news), "instruments": dict(self.latest)}
