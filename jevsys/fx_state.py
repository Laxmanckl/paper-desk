"""Stage 1: the state engine for gold + forex (guide Step 3, adapted).

At each decision time t it turns quotes and closed M5 bars into a compact,
numbers-only snapshot per instrument. All math lives here so no Jev call is
spent on arithmetic.

Lookahead guard, part 2: only bars that closed at or before t and quotes
stamped at or before t are used. Asking for the same t later, after newer data
arrived, returns the identical snapshot (a test checks this).

Retail MT5 brokers give no real order book, so the guide's depth, queue and
imbalance fields are replaced by quote-based ones: spread vs its normal level,
tick activity, trading session and a USD strength index across the majors.
"""
from __future__ import annotations

import json
import math
import statistics

from .book_state import AccountState
from .config import StateConfig
from .instruments import FX_MAJORS, INSTRUMENTS
from .market import MarketStore
from .types import BAR_SECONDS, Bar

SESSIONS = [  # UTC hours; numbers only in the snapshot
    (0, 7, 0, "Asia"),
    (7, 12, 1, "London"),
    (12, 16, 2, "London + New York"),
    (16, 21, 3, "New York"),
    (21, 24, 4, "Late / rollover"),
]


def session_at(t: float) -> tuple[int, int, str]:
    """(code, minutes into session, name) for a UTC timestamp."""
    h = (t % 86_400) / 3600
    for start, end, code, name in SESSIONS:
        if start <= h < end:
            return code, int((h - start) * 60), name
    return 4, 0, "Late / rollover"


def compact_json(snap: dict) -> str:
    return json.dumps(snap, separators=(",", ":"))


def estimate_tokens(snap: dict) -> int:
    """Conservative: ~3 chars per token for dense numeric JSON."""
    return math.ceil(len(compact_json(snap)) / 3)


class SnapshotTooLarge(ValueError):
    pass


def _r(x, nd=2):
    return None if x is None or not math.isfinite(x) else round(x, nd)


def _ret_bps(bars: list[Bar], t: float, horizon_s: float) -> float | None:
    if not bars:
        return None
    target = t - horizon_s
    past = None
    for b in reversed(bars):  # newest first; small horizons exit early
        if b.close_ts <= target:
            past = b.close
            break
    return None if past is None else (bars[-1].close / past - 1) * 1e4


def _true_ranges(bars: list[Bar]) -> list[float]:
    return [max(b.high, p.close) - min(b.low, p.close) for p, b in zip(bars, bars[1:])]


def _ema(values: list[float], n: int) -> list[float]:
    k, out = 2 / (n + 1), []
    for v in values:
        out.append(v if not out else out[-1] + k * (v - out[-1]))
    return out


def _rsi(closes: list[float], n: int) -> float | None:
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[:n], closes[1 : n + 1]):
        d = b - a
        gains, losses = gains + max(d, 0), losses + max(-d, 0)
    ag, al = gains / n, losses / n
    for a, b in zip(closes[n:], closes[n + 1 :]):
        d = b - a
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return 100 - 100 / (1 + ag / al)


class FxStateEngine:
    def __init__(self, market: MarketStore, cfg: StateConfig | None = None, starting_equity: float = 10_000.0):
        self.market = market
        self.cfg = cfg or StateConfig()
        self.accounts = {s: AccountState(0.0) for s in INSTRUMENTS}  # per-symbol P&L; equity tracked by desk
        self.starting_equity = starting_equity
        self.peak_equity = starting_equity

    # ---- one instrument -------------------------------------------------
    def snapshot(self, symbol: str, t: float) -> dict:
        cfg, spec = self.cfg, INSTRUMENTS[symbol]
        pip = spec["pip"]
        with self.market.lock:
            sd = self.market.data[symbol]
            bars = sd.bars_closed_by(t)[-1_500:]
            q = sd.quote_at(t)
            point = sd.point or 10 ** -spec["digits"]
        last = bars[-1] if bars else None
        mid = q.mid if q else (last.close if last else None)
        if mid is None:
            raise ValueError(f"{symbol}: no data yet")

        # spread now vs its normal level over the last day
        spread_pips = (q.ask - q.bid) / pip if q else None
        day_spreads = [b.spread_pts * point / pip for b in bars[-288:] if b.spread_pts > 0]
        norm_spread = statistics.median(day_spreads) if day_spreads else None

        # volatility
        trs = _true_ranges(bars[-(cfg.atr_period * 120 + 1):])
        atr = sum(trs[-cfg.atr_period:]) / cfg.atr_period if len(trs) >= cfg.atr_period else None
        atr_5d = sum(trs) / len(trs) if len(trs) >= 288 else None

        # trend
        closes = [b.close for b in bars[-400:]]
        ema_f = _ema(closes, cfg.ema_fast) if len(closes) >= cfg.ema_fast * 2 else None
        ema_s = _ema(closes, cfg.ema_slow) if len(closes) >= cfg.ema_slow * 2 else None
        dist = lambda e: (closes[-1] - e) / atr if atr else None  # noqa: E731

        # today's range (UTC day)
        day0 = t - t % 86_400
        today = [b for b in bars if b.ts >= day0]
        hi = max((b.high for b in today), default=None)
        lo = min((b.low for b in today), default=None)
        range_pos = (closes[-1] - lo) / (hi - lo) if today and hi > lo else None

        # activity: last hour's ticks vs the average hour over the last 5 days
        tv_hour = sum(b.tick_volume for b in bars[-12:])
        hours = [sum(b.tick_volume for b in bars[i : i + 12]) for i in range(max(0, len(bars) - 1440), len(bars) - 12, 12)]
        hours = [h for h in hours if h > 0]
        tv_ratio = tv_hour / statistics.mean(hours) if hours and len(bars) >= 300 else None

        sess, sess_min, _ = session_at(t)
        acct = self.accounts[symbol]
        snap = {
            "gold": 1 if spec["kind"] == "gold" else 0,
            # price
            "mid": round(mid, spec["digits"]),
            "ret_15m_bps": _r(_ret_bps(bars, t, 900)),
            "ret_1h_bps": _r(_ret_bps(bars, t, 3_600)),
            "ret_4h_bps": _r(_ret_bps(bars, t, 14_400)),
            "ret_1d_bps": _r(_ret_bps(bars, t, 86_400)),
            # cost
            "spread_pips": _r(spread_pips, 1),
            "spread_vs_norm": _r(spread_pips / norm_spread, 2) if spread_pips is not None and norm_spread else None,
            # volatility
            "atr_pips": _r(atr / pip, 1) if atr else None,
            "atr_vs_5d": _r(atr / atr_5d, 2) if atr and atr_5d else None,
            "day_range_pips": _r((hi - lo) / pip, 1) if today else None,
            "range_pos": _r(range_pos, 2),
            # trend / momentum
            "ema20_dist_atr": _r(dist(ema_f[-1]), 2) if ema_f and atr else None,
            "ema100_dist_atr": _r(dist(ema_s[-1]), 2) if ema_s and atr else None,
            "ema20_slope_atr": _r((ema_f[-1] - ema_f[-4]) / atr, 3) if ema_f and atr else None,
            "rsi14": _r(_rsi(closes[-200:], cfg.rsi_period), 1),
            # activity + clock
            "tickvol_ratio": _r(tv_ratio, 2),
            "session": sess,
            "session_min": sess_min,
            # dollar
            "usd_1h_bps": _r(self.usd_index(t, 3_600)),
            "usd_1d_bps": _r(self.usd_index(t, 86_400)),
            # our book
            "pos": acct.position,
            "upnl_pips": _r(acct.unrealized(mid) / pip if acct.position else 0.0, 1),
            "held_min": _r(acct.held_seconds(t) / 60, 0),
            "dd": _r(self.drawdown(), 4),
            # data health
            "quote_age_s": _r(t - q.ts, 1) if q else None,
            "bar_age_s": _r(t - last.close_ts, 0) if last else None,
            "bars_24h": sum(1 for b in bars if b.close_ts > t - 86_400),
        }
        tok = estimate_tokens(snap)
        if tok > cfg.max_snapshot_tokens:
            raise SnapshotTooLarge(f"{symbol}: ~{tok} tokens > {cfg.max_snapshot_tokens}")
        return snap

    # ---- cross-market ---------------------------------------------------
    def usd_index(self, t: float, horizon_s: float) -> float | None:
        """Average dollar move across the majors, in bps (+ = USD stronger)."""
        moves = []
        with self.market.lock:
            for s in FX_MAJORS:
                r = _ret_bps(self.market.data[s].bars_closed_by(t), t, horizon_s)
                if r is not None:
                    moves.append(INSTRUMENTS[s]["usd_sign"] * r)
        return statistics.mean(moves) if len(moves) >= 3 else None

    def drawdown(self) -> float:
        return 0.0  # Stage 1 holds no positions; the risk engine (Stage 4) tracks equity

    @staticmethod
    def is_fresh(snap: dict, cfg: StateConfig) -> tuple[bool, str]:
        """Guide Step 6: never act on stale information."""
        if snap.get("quote_age_s") is None:
            return False, "no live quote"
        if snap["quote_age_s"] > cfg.max_quote_age_s:
            return False, f"quote {snap['quote_age_s']:.0f}s old (market closed or bridge down)"
        if snap.get("bar_age_s") is None or snap["bar_age_s"] > BAR_SECONDS + cfg.grace_s + 60:
            return False, "latest 5-min bar missing"
        return True, "fresh"
