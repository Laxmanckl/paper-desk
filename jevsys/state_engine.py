"""Stage 1: the state engine (guide Step 3).

On every block it turns raw venue data into a compact, numbers-only snapshot
for Jev. All math lives here so no Jev call is ever spent on arithmetic.

Lookahead guard: any input stamped after the block timestamp raises
LookaheadError, and blocks must arrive in time order. Nothing in the snapshot
can depend on data from after the decision point.
"""
from __future__ import annotations

import bisect
import json
import math
from collections import deque

from .book_state import AccountState
from .config import StateConfig
from .types import BlockInput, OrderBook


class LookaheadError(ValueError):
    """Raised when an input carries information from after the block."""


class SnapshotTooLarge(ValueError):
    pass


def estimate_tokens(snapshot: dict) -> int:
    """Conservative token estimate for the compact JSON Jev receives.

    Numeric JSON tokenizes at roughly 3 chars/token; using 3 keeps us safely
    under the real budget.
    """
    return math.ceil(len(compact_json(snapshot)) / 3)


def compact_json(snapshot: dict) -> str:
    return json.dumps(snapshot, separators=(",", ":"))


def _r(x: float | None, nd: int = 4) -> float | None:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return None
    return round(x, nd)


def _sig(x: float | None, sig: int = 4) -> float | None:
    """Round to significant figures (keeps tiny vols readable, big prices short)."""
    if x is None or not math.isfinite(x):
        return None
    if x == 0:
        return 0.0
    return round(x, sig - 1 - int(math.floor(math.log10(abs(x)))))


class StateEngine:
    def __init__(self, cfg: StateConfig | None = None, starting_equity: float = 10_000.0):
        self.cfg = cfg or StateConfig()
        self.account = AccountState(starting_equity)
        self._last_ts: float | None = None
        self._last_block: int | None = None
        # block-level history only needs the longest return horizon / medium vol window;
        # the 24h baseline is kept separately as per-minute returns
        self._horizon = max(self.cfg.vol_medium_s, max(self.cfg.return_horizons_s))
        self._mid_ts: deque[float] = deque()
        self._mid_px: deque[float] = deque()
        self._trades: deque[tuple[float, float, str]] = deque()   # (ts, size, aggressor)
        self._cancels: deque[tuple[float, int]] = deque()
        self._orders: deque[tuple[int, int, int]] = deque()       # (sent, rejected, filled)
        self._slippage_bps: deque[float] = deque(maxlen=50)
        self._latencies_ms: deque[float] = deque(maxlen=self.cfg.latency_samples)
        self._minute_returns: deque[tuple[float, float]] = deque()  # (ts, log return) for 24h baseline
        self._last_minute: tuple[int, float] | None = None          # (minute index, mid)

    # ---- external hooks -------------------------------------------------
    def record_latency(self, ms: float) -> None:
        """Called by the decision layer (stage 2) after each Jev round trip."""
        self._latencies_ms.append(float(ms))

    # ---- main entry -----------------------------------------------------
    def on_block(self, inp: BlockInput) -> dict:
        book = inp.book
        now = book.ts
        self._check_time(inp)
        self._validate_book(book)

        best_bid, best_ask = book.bids[0], book.asks[0]
        mid = (best_bid.price + best_ask.price) / 2
        spread = best_ask.price - best_bid.price
        top = best_bid.size + best_ask.size
        microprice = (best_bid.price * best_ask.size + best_ask.price * best_bid.size) / top if top > 0 else mid

        # history updates use only data at or before `now`
        self._mid_ts.append(now)
        self._mid_px.append(mid)
        self._update_minute_returns(now, mid)
        for t in inp.trades:
            self._trades.append((t.ts, t.size, t.aggressor))
        if inp.cancels:
            self._cancels.append((now, inp.cancels))
        for f in inp.fills:
            self.account.apply_fill(f)
            if f.expected_price > 0:
                sign = 1 if f.side == "buy" else -1
                self._slippage_bps.append(sign * (f.price - f.expected_price) / f.expected_price * 1e4)
        if inp.orders_sent or inp.orders_rejected or inp.fills:
            self._orders.append((inp.orders_sent, inp.orders_rejected, len(inp.fills)))
            while len(self._orders) > self.cfg.health_window:
                self._orders.popleft()
        self._evict(now)

        drawdown = self.account.mark(mid)
        snap = {
            "blk": book.block,
            # price
            "mid": _sig(mid, 8),
            "micro_bps": _r((microprice - mid) / mid * 1e4, 2),
            **{f"ret_{h // 60}m_bps": _r(self._ret_bps(now, mid, h), 2) for h in self.cfg.return_horizons_s},
            # order book
            "spread_bps": _r(spread / mid * 1e4, 2),
            **self._depth(book),
            "imb": _r(self._imbalance(book), 3),
            **self._queue(inp),
            # flow
            **self._flow(now),
            # volatility
            **self._vol(now),
            # cross-market
            **self._cross(inp, mid),
            # our book
            "inv": _sig(self.account.position, 6),
            "upnl": _r(self.account.unrealized(mid), 2),
            "rpnl": _r(self.account.realized_pnl, 2),
            "dd": _r(drawdown, 4),
            "held_s": _r(self.account.held_seconds(now), 1),
            # system health
            **self._health(),
        }
        tokens = estimate_tokens(snap)
        if tokens > self.cfg.max_snapshot_tokens:
            raise SnapshotTooLarge(f"snapshot ~{tokens} tokens > {self.cfg.max_snapshot_tokens}")
        return snap

    # ---- guards ---------------------------------------------------------
    def _check_time(self, inp: BlockInput) -> None:
        now = inp.book.ts
        if self._last_ts is not None and now < self._last_ts:
            raise LookaheadError(f"block ts {now} earlier than previous {self._last_ts}")
        if self._last_block is not None and inp.book.block <= self._last_block:
            raise LookaheadError(f"block {inp.book.block} not after {self._last_block}")
        for t in inp.trades:
            if t.ts > now:
                raise LookaheadError(f"trade at {t.ts} is after block ts {now}")
        for f in inp.fills:
            if f.ts > now:
                raise LookaheadError(f"fill at {f.ts} is after block ts {now}")
        if inp.ref_ts is not None and inp.ref_ts > now:
            raise LookaheadError(f"reference price at {inp.ref_ts} is after block ts {now}")
        self._last_ts, self._last_block = now, inp.book.block

    @staticmethod
    def _validate_book(book: OrderBook) -> None:
        if not book.bids or not book.asks:
            raise ValueError("order book needs at least one bid and one ask")
        if book.bids[0].price >= book.asks[0].price:
            raise ValueError("crossed or locked book")

    # ---- feature groups -------------------------------------------------
    def _evict(self, now: float) -> None:
        cutoff = now - self._horizon - 1
        while self._mid_ts and self._mid_ts[0] < cutoff:
            self._mid_ts.popleft()
            self._mid_px.popleft()
        while self._minute_returns and self._minute_returns[0][0] < now - self.cfg.vol_baseline_s:
            self._minute_returns.popleft()
        flow_cut = now - max(self.cfg.flow_window_s, self.cfg.vol_short_s)
        while self._trades and self._trades[0][0] < flow_cut:
            self._trades.popleft()
        while self._cancels and self._cancels[0][0] < now - self.cfg.flow_window_s:
            self._cancels.popleft()

    def _mid_at_or_before(self, t: float) -> float | None:
        i = bisect.bisect_right(self._mid_ts, t) - 1
        return self._mid_px[i] if i >= 0 else None

    def _ret_bps(self, now: float, mid: float, horizon: float) -> float | None:
        past = self._mid_at_or_before(now - horizon)
        return None if past is None else (mid / past - 1) * 1e4

    def _depth(self, book: OrderBook) -> dict:
        n = self.cfg.depth_levels
        out = {}
        for i in range(n):
            out[f"bd{i + 1}"] = _sig(book.bids[i].size) if i < len(book.bids) else 0.0
            out[f"ad{i + 1}"] = _sig(book.asks[i].size) if i < len(book.asks) else 0.0
        return out

    def _imbalance(self, book: OrderBook) -> float:
        n = self.cfg.depth_levels
        b = sum(l.size for l in book.bids[:n])
        a = sum(l.size for l in book.asks[:n])
        return 0.0 if b + a == 0 else (b - a) / (b + a)  # -1 all asks .. +1 all bids

    def _queue(self, inp: BlockInput) -> dict:
        out = {"q_bid": None, "q_ask": None}
        for q in inp.own_quotes:
            # fraction of the level that sits ahead of us (0 = front of queue)
            total = q.size_ahead + q.size
            frac = q.size_ahead / total if total > 0 else 0.0
            out["q_bid" if q.side == "buy" else "q_ask"] = _r(frac, 3)
        return out

    def _flow(self, now: float) -> dict:
        w = self.cfg.flow_window_s
        buy = sell = 0.0
        n = 0
        for ts, size, side in self._trades:
            if ts >= now - w:
                n += 1
                if side == "buy":
                    buy += size
                else:
                    sell += size
        canc = sum(c for ts, c in self._cancels if ts >= now - w)
        tot = buy + sell
        return {
            "agg_buy": _sig(buy),
            "agg_sell": _sig(sell),
            "agg_buy_ratio": _r(buy / tot, 3) if tot > 0 else None,
            "trades_ps": _r(n / w, 2),
            "cancels_ps": _r(canc / w, 2),
        }

    def _update_minute_returns(self, now: float, mid: float) -> None:
        minute = int(now // 60)
        if self._last_minute is None:
            self._last_minute = (minute, mid)
            return
        last_min, last_mid = self._last_minute
        if minute > last_min:
            self._minute_returns.append((now, math.log(mid / last_mid)))
            self._last_minute = (minute, mid)

    def _realized_vol(self, now: float, window: float) -> float | None:
        """Std of log returns between consecutive block mids in the window,
        scaled to a per-minute figure so windows are comparable."""
        i = bisect.bisect_left(self._mid_ts, now - window)
        pts = [(self._mid_ts[j], self._mid_px[j]) for j in range(i, len(self._mid_ts))]
        if len(pts) < 3:
            return None
        rets, dts = [], []
        for (t0, m0), (t1, m1) in zip(pts, pts[1:]):
            if t1 > t0:
                rets.append(math.log(m1 / m0))
                dts.append(t1 - t0)
        if len(rets) < 2:
            return None
        var_per_s = sum(r * r for r in rets) / sum(dts)
        return math.sqrt(var_per_s * 60)

    def _vol(self, now: float) -> dict:
        short = self._realized_vol(now, self.cfg.vol_short_s)
        med = self._realized_vol(now, self.cfg.vol_medium_s)
        base = None
        if len(self._minute_returns) >= 30:
            r = [x for _, x in self._minute_returns]
            base = math.sqrt(sum(x * x for x in r) / len(r))
        return {
            "vol_s_bps": _r(short * 1e4, 3) if short is not None else None,
            "vol_m_bps": _r(med * 1e4, 3) if med is not None else None,
            "vol_vs_24h": _r(med / base, 3) if med is not None and base else None,
        }

    def _cross(self, inp: BlockInput, mid: float) -> dict:
        ref_gap = None
        if inp.ref_price and inp.ref_ts is not None and inp.book.ts - inp.ref_ts <= self.cfg.max_ref_age_s:
            ref_gap = (mid / inp.ref_price - 1) * 1e4
        basis = (mid / inp.index_price - 1) * 1e4 if inp.index_price else None
        return {
            "ref_gap_bps": _r(ref_gap, 2),
            "basis_bps": _r(basis, 2),
            "funding": _sig(inp.funding_rate) if inp.funding_rate is not None else None,
        }

    def _health(self) -> dict:
        sent = sum(o[0] for o in self._orders)
        rej = sum(o[1] for o in self._orders)
        filled = sum(o[2] for o in self._orders)
        lat = list(self._latencies_ms)
        return {
            "fill_ratio": _r(filled / sent, 3) if sent else None,
            "reject_ratio": _r(rej / sent, 3) if sent else None,
            "slip_bps": _r(sum(self._slippage_bps) / len(self._slippage_bps), 2) if self._slippage_bps else None,
            "lat_ms": [round(x) for x in lat],
        }
