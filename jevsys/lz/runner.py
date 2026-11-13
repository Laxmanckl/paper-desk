"""Runs the Lorentzian Classification model on live hourly bars and trades its paper account.

One model per instrument, fed one closed hourly bar at a time (``lorentzian.py``).
When the model starts a long or a short on the bar that just closed, the position
is opened at the live bid/ask, which is the next bar's open.

Rules around the model (ours; the TradingView script has no stops or sizing):
  * exit after ``hold_bars`` hourly bars (the model's own 4-bar horizon), or earlier
    when the model starts a trade the other way (that trade is then taken)
  * protective stop ``stop_atr_mult`` x hourly ATR(14); target ``target_atr_mult`` x ATR
  * risk ``risk_per_trade_pct`` of equity to the stop, at most ``max_open_positions`` open
  * no entry within ``news_window_min`` of a listed release for either currency
  * after a 2% losing day or 4 losses in a row, no new entries until the next UTC day
  * its own drawdown kill switch
Paper only: no order ever reaches the broker.

Not validated. The same model was tested on 15-minute bars (-36% in 6 weeks) and on
daily bars (no edge, 2015-2023) and failed both; hourly bars are untested.
"""
from __future__ import annotations

import math
import os
import time

import pandas as pd

from ..instruments import INSTRUMENTS, pip_value_usd
from ..paper import exit_price
from ..risk import RiskEngine, notional_usd, size_lots
from ..vbo.lib.news import load_news_filter, symbol_currencies
from .lorentzian import Lorentzian

STRATEGY = "lorentzian"
LABEL = "Lorentzian ML"
H = 3600
MIN_H1_BARS = 1_000          # indicators, the regime filter's 200-bar average and the memory are settled
PREDICT_LAST = 2_000         # when loading history, search neighbours only for the newest bars (as TradingView does)
FRESH_S = 15 * 60            # a bar older than this when it arrives is history, not a signal
TAIL = 50                    # fed bars remembered, to notice history that arrived out of order
ATR_PERIOD = 14
MIN_SL = {"XAUUSD": 0.50}
MIN_SL_DEFAULT = 0.00020
NEWS_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "config", "news_blackout.csv")


def _utc_day(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(t))


def atr(bars, period: int = ATR_PERIOD) -> float | None:
    """Wilder's ATR of the latest bars (the last ~10 periods are enough to settle it)."""
    bars = bars[-(period * 10 + 1):]
    if len(bars) < period + 1:
        return None
    trs = [max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close)) for p, b in zip(bars, bars[1:])]
    v = sum(trs[:period]) / period
    for tr in trs[period:]:
        v = (v * (period - 1) + tr) / period
    return v


class LzRisk(RiskEngine):
    """The Lorentzian account's own limits and exit rules. Reads its settings at every call."""

    @property
    def r(self) -> dict:
        return self.s.lz

    def exit_reason(self, pos, q, now: float) -> str | None:
        px = exit_price(pos.side, q)
        if (pos.dir == 1 and px <= pos.stop) or (pos.dir == -1 and px >= pos.stop):
            return "stop"
        if (pos.dir == 1 and px >= pos.target) or (pos.dir == -1 and px <= pos.target):
            return "target"
        hold = int(self.r["hold_bars"])
        if now >= math.floor(pos.opened / H) * H + hold * H:     # the close of the hold-th bar after entry
            return f"{hold} bars held (the model's horizon)"
        g = time.gmtime(now)
        if g.tm_wday == 4 and g.tm_hour * 60 + g.tm_min >= self.s.minutes("friday_flat_utc"):
            return "flat before the weekend"
        return None

    def kill_check(self, m: dict) -> tuple[str, str] | None:
        if m["drawdown_pct"] >= self.r["max_drawdown_pct"]:
            return "drawdown", f"equity {m['drawdown_pct']:.2f}% below peak (max {self.r['max_drawdown_pct']}%)"
        if m["daily_loss_pct"] >= self.r["max_daily_loss_pct"]:
            return "daily", f"down {m['daily_loss_pct']:.2f}% today (max {self.r['max_daily_loss_pct']}%)"
        return None


class LorentzianAccount:
    label = LABEL

    def __init__(self, market, trader, settings, news_file: str = NEWS_FILE):
        self.market, self.trader, self.settings = market, trader, settings
        self.news = load_news_filter(news_file, int(self.cfg["news_window_min"]))
        self.models: dict[str, dict] = {}      # per symbol: the model and which bars it has been fed
        self.done: dict[str, tuple] = {}       # (newest bar open time, bar count) last looked at, per symbol
        self.latest: dict[str, dict] = {}      # what the dashboard shows per instrument
        self.evaluations = 0

    @property
    def cfg(self) -> dict:
        return self.settings.lz

    @property
    def book(self):
        return self.trader.book

    # ---- the model, kept in step with the bar history --------------------------
    def _feed(self, sym: str, bars) -> tuple[dict | None, bool]:
        """Feed the model every closed bar it has not seen. Returns (the newest bar's
        output, whether the model was rebuilt from the whole history)."""
        st = self.models.get(sym)
        ts = [b.ts for b in bars]
        new_from = None
        if st:
            tail = st["tail"]
            i = len(ts) - 1
            while i >= 0 and ts[i] > tail[-1]:
                i -= 1
            # the bars up to the last one fed must still be the ones that were fed
            same = i >= 0 and ts[max(0, i + 1 - len(tail)):i + 1] == tail[-(i + 1):] and ts[0] >= st["oldest"]
            new_from = i + 1 if same else None
        rebuilt = new_from is None
        if rebuilt:
            st = self.models[sym] = {"model": Lorentzian(), "tail": [], "oldest": ts[0], "out": None}
            new_from = 0
        model, cut = st["model"], len(bars) - PREDICT_LAST
        for j in range(new_from, len(bars)):
            b = bars[j]
            st["out"] = model.update(b.open, b.high, b.low, b.close, predict=not rebuilt or j >= cut)
        st["tail"] = (st["tail"] + ts[new_from:])[-TAIL:]
        return st["out"], rebuilt

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
        loaded = False
        for sym, bars in series.items():
            last = bars[-1]
            if len(bars) < MIN_H1_BARS:
                self.done[sym] = (last.ts, len(bars))
                self.latest[sym] = {"status": "waiting", "bars": len(bars), "t": last.ts + H,
                                    "why": f"loading hourly history ({len(bars)} of {MIN_H1_BARS} bars)"}
                continue
            st = self.models.get(sym)
            if loaded and (st is None or len(bars) - self.done.get(sym, (0, 0))[1] > TAIL):
                continue                       # one full history load per tick; the rest follow in 2 s
            out, rebuilt = self._feed(sym, bars)
            loaded = loaded or rebuilt
            self.done[sym] = (last.ts, len(bars))
            rec = {"bars": len(bars), "t": last.ts + H, "prediction": out["prediction"], "signal": out["signal"]}
            if now - (last.ts + H) > FRESH_S:
                self.latest[sym] = {**rec, "status": "idle",
                                    "why": "latest hourly bar is old (market closed or bridge catching up)"}
            elif not v["enabled"]:
                self.latest[sym] = {**rec, "status": "off", "why": f"{LABEL} switched off in config/desk.json"}
            else:
                self.latest[sym] = {**rec, **self.evaluate(sym, bars, out, now, quotes.get(sym))}

    # ---- one closed bar ----------------------------------------------------------
    def evaluate(self, sym: str, bars, out: dict, now: float, q) -> dict:
        self.evaluations += 1
        side = "long" if out["start_long"] else "short" if out["start_short"] else None
        pos = self.book.positions.get(sym)
        if side is None:
            why = self._why_not(out)
            return {"status": "in position" if pos else "stand aside", "why": why, **({"side": pos.side} if pos else {})}
        if pos and pos.side == side:
            return {"status": "in position", "side": side, "why": "model repeats its signal: already in this trade"}
        if pos and q is not None and now - q.ts <= self.cfg["max_quote_age_s"]:
            self.trader.close(sym, q, now, f"model flipped to {side}", self.settings.alerts)
            pos = None
        vetoes = self.vetoes(sym, side, bars, now, q)
        if vetoes:
            why = "; ".join(vetoes)
            self.trader.event(f"{sym} {side} signal skipped: {why}")
            return {"status": "skipped", "side": side, "why": why}
        p = self.open(sym, side, bars, out, now, q)
        if p is None:
            return {"status": "skipped", "side": side, "why": "position size below 0.01 lots"}
        return {"status": "entered", "side": side, "why": p.reason, "position_id": p.id}

    @staticmethod
    def _why_not(out: dict) -> str:
        if not out["warm"]:
            return "indicators still warming up"
        p, s = out["prediction"], out["signal"]
        lean = "long" if s == 1 else "short" if s == -1 else "no side"
        if not out["volatility_ok"] or not out["regime_ok"]:
            off = [n for n, ok in (("volatility", out["volatility_ok"]), ("trend regime", out["regime_ok"])) if not ok]
            return f"model score {p:+d}; {' and '.join(off)} filter not met, so the signal stays {lean}"
        return f"model score {p:+d}: still {lean}, no new signal on this bar (or the kernel line disagrees)"

    def vetoes(self, sym: str, side: str, bars, now: float, q) -> list[str]:
        v, book, out = self.cfg, self.book, []
        today = _utc_day(now)
        g = time.gmtime(now)
        if book.halted:
            out.append(f"kill switch on ({book.halted['reason']})")
        if sym in book.positions:
            out.append("already in a position (price too old to close it)")
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
        a = atr(bars)
        if q is None or now - q.ts > v["max_quote_age_s"]:
            out.append("price data too old")
        elif a is None or v["stop_atr_mult"] * a < max(MIN_SL.get(sym, MIN_SL_DEFAULT), 2 * (q.ask - q.bid)):
            out.append("stop closer than the minimum distance / twice the spread")
        if g.tm_wday == 4 and g.tm_hour * 60 + g.tm_min >= self.settings.minutes("friday_no_entries_utc") \
                or g.tm_wday in (5, 6):
            out.append("weekend close: no new positions")
        return out

    def open(self, sym: str, side: str, bars, out: dict, now: float, q):
        v, spec = self.cfg, INSTRUMENTS[sym]
        d = 1 if side == "long" else -1
        a = atr(bars)
        entry = q.ask if d == 1 else q.bid
        stop = round(entry - d * v["stop_atr_mult"] * a, spec["digits"])
        target = round(entry + d * v["target_atr_mult"] * a, spec["digits"])
        stop_pips = abs(entry - stop) / spec["pip"]
        equity = self.trader.last_mark.get("equity") or self.book.start_equity
        lots = size_lots(equity, v["risk_per_trade_pct"], stop_pips, pip_value_usd(sym, entry), v["max_lots"])
        room = max(0.0, v["max_leverage"] * equity - sum(notional_usd(s, p.lots, p.entry)
                                                          for s, p in self.book.positions.items()))
        per_lot = notional_usd(sym, 1.0, entry)
        lots = round(min(lots, math.floor(room / per_lot * 100) / 100 if per_lot > 0 else 0.0), 2)
        if lots < 0.01:
            self.trader.event(f"{sym} {side} signal skipped: position size below 0.01 lots")
            return None
        why = f"{LABEL} {side} · model score {out['prediction']:+d} of 8"
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
        return {"strategy": STRATEGY, "label": LABEL, "enabled": v["enabled"], "evaluations": self.evaluations,
                "params": {k: v[k] for k in ("hold_bars", "stop_atr_mult", "target_atr_mult", "risk_per_trade_pct",
                                             "max_open_positions")},
                "news_events": len(self.news), "instruments": dict(self.latest)}
