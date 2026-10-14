"""The desk: runs the block loop and keeps everything the dashboard shows.

Stage 1 wires the state engine only. Stages 2-6 plug into `_decide()`; until
then the Jev, policy, risk and fallback panels report "pending" and the desk
never places an order (paper only, and not even paper orders yet).
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time
from collections import deque

from .config import StateConfig
from .feeds.base import Feed
from .snapshot_log import SnapshotLog
from .state_engine import LookaheadError, SnapshotTooLarge, StateEngine, estimate_tokens

MODEL_VERSION = "jev-1.13.0"  # pinned (guide Step 2). Used from Stage 2.

STAGES = [
    {"n": 1, "name": "State engine", "what": "Numeric snapshot < 400 tokens, no future data", "status": "live"},
    {"n": 2, "name": "Jev decision layer", "what": "6-question battery, direct API, pinned version", "status": "pending"},
    {"n": 3, "name": "Policy engine", "what": "Confidence thresholds in config", "status": "pending"},
    {"n": 4, "name": "Risk engine", "what": "Hard limits, veto on every order", "status": "pending"},
    {"n": 5, "name": "Block loop + fallback ladder", "what": "Late = hold, Jev down = code rules, limit = kill", "status": "pending"},
    {"n": 6, "name": "Paper mode + calibration log", "what": "Two weeks before real money", "status": "pending"},
]

QUESTIONS = [
    {"key": "regime", "type": "Choice", "ask": "What regime is the market in right now",
     "options": ["trending", "mean_reverting", "high_vol", "crisis"]},
    {"key": "direction", "type": "Choice", "ask": "Most likely price bias over the next 10 blocks",
     "options": ["up", "down", "neutral"]},
    {"key": "toxic_flow", "type": "Noul", "ask": "Aggressive flow is likely informed traders, not noise"},
    {"key": "liquidity_stressed", "type": "Noul", "ask": "The order book is thinner than its normal level"},
    {"key": "quote_environment", "type": "Score", "ask": "How favorable is this state for providing liquidity",
     "options": ["No", "Marginal", "Standard", "Excellent"]},
    {"key": "inventory_pressure", "type": "Score", "ask": "How urgently inventory should be reduced",
     "options": ["None", "Mild", "Skew hard", "Reduce now"]},
]

RISK_LIMITS = ["Max position size", "Max daily loss", "Max drawdown", "Max order size",
               "Max time holding inventory", "Max age of market data", "Max leverage",
               "Max API errors in a row", "Max decision response time"]

LADDER = [
    {"key": "normal", "name": "Healthy, high confidence", "do": "Normal operation"},
    {"key": "cautious", "name": "Healthy, low confidence", "do": "Trade smaller, or just watch"},
    {"key": "late", "name": "Answer arrived late", "do": "Hold, no stale orders"},
    {"key": "fallback", "name": "Jev unavailable", "do": "Simple code-only rules"},
    {"key": "kill", "name": "Hard limit breached", "do": "Kill switch, flatten, alert"},
]

SERIES_EVERY_S = 30      # one chart point per 30 s
SERIES_KEEP_S = 86_400   # 24 h of chart history
LOG_KEEP = 150


class Desk:
    def __init__(self, feed: Feed, state_path: str = "state/jev_desk.json", runner: str = "local",
                 log_dir: str = "logs", starting_equity: float = 10_000.0, cfg: StateConfig | None = None):
        self.feed, self.state_path, self.runner = feed, state_path, runner
        self.engine = StateEngine(cfg, starting_equity)
        self.snaplog = SnapshotLog(log_dir)
        self.lock = threading.Lock()
        self.started = time.time()
        self.blocks = 0
        self.errors = 0
        self.rejected = 0
        self.last_block_ts: float | None = None
        self.last_snapshot: dict | None = None
        self.last_tokens = 0
        self.max_tokens = 0
        self.engine_ms: deque[float] = deque(maxlen=2000)
        self.lag_ms: deque[float] = deque(maxlen=2000)
        self.series: deque[list] = deque()
        self.equity: deque[list] = deque()
        self.log: deque[list] = deque(maxlen=LOG_KEEP)
        self.created = self.started
        self.feed_status, self.feed_detail = "starting", ""
        self._last_series_t = 0.0
        self._restore()
        self.event(f"Desk started ({runner}). Feed: {feed.label or feed.name}. "
                   "Snapshots warm up for 30 min (returns) and 24 h (vol baseline).")

    # ---- persistence ----------------------------------------------------
    def _restore(self) -> None:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (OSError, ValueError):
            return
        now = time.time()
        self.created = old.get("created", self.started)
        self.series.extend(p for p in old.get("series", []) if now - p[0] <= SERIES_KEEP_S)
        self.equity.extend(p for p in old.get("equity", []) if now - p[0] <= SERIES_KEEP_S)
        self.log.extend(old.get("log", [])[-LOG_KEEP:])
        tot = old.get("totals", {})
        self.blocks_before = tot.get("blocks", 0)

    def save(self) -> str:
        data = self.state(full=True)
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
        os.replace(tmp, self.state_path)
        return self.state_path

    # ---- events ---------------------------------------------------------
    def event(self, text: str) -> None:
        with self.lock:
            self.log.append([round(time.time(), 1), text])

    # ---- the loop -------------------------------------------------------
    def step(self, blk) -> dict | None:
        """Process one block. Returns the snapshot, or None if the block was rejected."""
        t0 = time.perf_counter()
        try:
            snap = self.engine.on_block(blk)
        except LookaheadError as e:
            with self.lock:
                self.rejected += 1
            self.event(f"Block {blk.book.block} rejected: {e}")
            return None
        except (SnapshotTooLarge, ValueError) as e:
            with self.lock:
                self.errors += 1
            self.event(f"Block {blk.book.block} skipped: {e}")
            return None
        ms = (time.perf_counter() - t0) * 1000
        tokens = estimate_tokens(snap)
        self.snaplog.write(blk.book.ts, {"snap": snap, "tokens": tokens, "model": None, "decision": None})
        self._decide(snap)
        acct = self.engine.account
        with self.lock:
            self.blocks += 1
            self.last_block_ts = blk.book.ts
            self.last_snapshot, self.last_tokens = snap, tokens
            self.max_tokens = max(self.max_tokens, tokens)
            self.engine_ms.append(ms)
            self.lag_ms.append(max(0.0, (time.time() - blk.book.ts) * 1000))
            self.feed_status = "live"
            if blk.book.ts - self._last_series_t >= SERIES_EVERY_S:
                self._last_series_t = blk.book.ts
                self.series.append([round(blk.book.ts), snap["mid"], snap["spread_bps"], snap["imb"], snap["vol_s_bps"]])
                self.equity.append([round(blk.book.ts), round(acct.equity(snap["mid"]), 2)])
                cut = blk.book.ts - SERIES_KEEP_S
                while self.series and self.series[0][0] < cut:
                    self.series.popleft()
                while self.equity and self.equity[0][0] < cut:
                    self.equity.popleft()
        return snap

    def _decide(self, snap: dict) -> None:
        """Stages 2-5 go here: Jev battery -> policy -> pricing -> risk veto.
        Stage 1: observe only, no orders."""
        return None

    def run(self, stop: threading.Event | None = None, realtime: bool = True) -> None:
        stop = stop or threading.Event()
        for blk in self.feed.blocks():
            if stop.is_set():
                break
            if realtime:
                wait = blk.book.ts - time.time()
                if wait > 0:
                    if stop.wait(wait):
                        break
            self.step(blk)
        self.snaplog.close()

    # ---- what the dashboard reads ---------------------------------------
    def state(self, full: bool = False) -> dict:
        with self.lock:
            snap = self.last_snapshot or {}
            acct = self.engine.account
            mid = snap.get("mid")
            em = list(self.engine_ms)
            lag = list(self.lag_ms)
            d = {
                "desk": {"name": "Jev Desk", "stage": 1, "model": MODEL_VERSION},
                "runner": self.runner,
                "server_time": time.time(),
                "created": self.created,
                "started": self.started,
                "feed": {"name": self.feed.name, "label": self.feed.label, "status": self.feed_status,
                         "detail": self.feed_detail},
                "last_block_ts": self.last_block_ts,
                "totals": {"blocks": self.blocks + getattr(self, "blocks_before", 0),
                           "blocks_session": self.blocks, "rejected": self.rejected, "errors": self.errors},
                "engine": {"p50_ms": _q(em, .5), "p95_ms": _q(em, .95), "max_ms": max(em) if em else None,
                           "lag_p50_ms": _q(lag, .5), "tokens": self.last_tokens, "max_tokens": self.max_tokens,
                           "budget": self.engine.cfg.max_snapshot_tokens},
                "snapshot": snap,
                "account": {"start": acct.starting_equity,
                            "equity": round(acct.equity(mid), 2) if mid else acct.starting_equity,
                            "realized": round(acct.realized_pnl, 2),
                            "unrealized": round(acct.unrealized(mid), 2) if mid else 0.0,
                            "position": acct.position, "drawdown": snap.get("dd", 0.0)},
                "stages": STAGES,
                "jev": {"status": "pending", "model": MODEL_VERSION, "questions": QUESTIONS, "answers": None,
                        "latency_ms": None},
                "policy": {"status": "pending", "action": None},
                "risk": {"status": "pending", "limits": [{"name": n, "value": None, "limit": None} for n in RISK_LIMITS]},
                "ladder": {"status": "pending", "rungs": LADDER, "current": None},
                "log": list(self.log)[-60:] if not full else list(self.log),
            }
            if full:
                d["series"] = list(self.series)
                d["equity"] = list(self.equity)
            return d


def _q(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    if len(xs) == 1:
        return round(xs[0], 3)
    return round(statistics.quantiles(xs, n=100, method="inclusive")[max(0, min(98, int(q * 100) - 1))], 3)
