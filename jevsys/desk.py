"""The desk: the decision loop for gold + forex and everything the dashboard shows.

Every `decide_every_min` minutes (15 by default), just after a 5-minute bar
closes, it builds a snapshot per instrument. Stage 1 stops there: snapshots are
checked for freshness and logged. Stages 2-5 plug into `_decide()` (Jev battery
-> policy -> risk veto). No orders exist yet, paper or otherwise.
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time
from collections import deque

from .config import StateConfig
from .fx_state import FxStateEngine, SnapshotTooLarge, estimate_tokens, session_at
from .instruments import INSTRUMENTS
from .market import MarketStore
from .snapshot_log import SnapshotLog

MODEL_VERSION = "jev-1.13.0"  # pinned (guide Step 2), used from Stage 2

STAGES = [
    {"n": 1, "name": "State engine", "what": "Snapshot per instrument, < 400 tokens, no future data", "status": "live"},
    {"n": 2, "name": "Jev decision layer", "what": "6-question battery per instrument, pinned version", "status": "pending"},
    {"n": 3, "name": "Policy engine", "what": "Long / short / exit / stand aside by confidence", "status": "pending"},
    {"n": 4, "name": "Risk engine", "what": "Hard limits, stop per trade, veto on every order", "status": "pending"},
    {"n": 5, "name": "Loop + fallback ladder", "what": "Stale = hold, Jev down = code rules, limit = kill", "status": "pending"},
    {"n": 6, "name": "Paper trading + calibration", "what": "Two weeks before any real money", "status": "pending"},
]

QUESTIONS = [
    {"key": "regime", "type": "Choice", "ask": "What regime is this market in right now",
     "options": ["trending", "ranging", "volatile", "quiet"]},
    {"key": "direction", "type": "Choice", "ask": "Most likely price bias over the next hour",
     "options": ["up", "down", "neutral"]},
    {"key": "move_continues", "type": "Noul", "ask": "The latest move is likely to continue rather than reverse"},
    {"key": "costs_abnormal", "type": "Noul", "ask": "Trading costs are abnormally high right now"},
    {"key": "entry_quality", "type": "Score", "ask": "How good is this moment to open a new position",
     "options": ["No", "Marginal", "Good", "Excellent"]},
    {"key": "exit_urgency", "type": "Score", "ask": "How urgently an open position should be closed",
     "options": ["None", "Mild", "Tighten stop", "Close now"]},
]

RISK_LIMITS = ["Max position size (lots)", "Max open positions", "Max risk per trade (stop)",
               "Max daily loss", "Max drawdown", "Max spread to enter", "Max age of price data",
               "Max Jev errors in a row", "Max decision response time", "No new trades around major news"]

LADDER = [
    {"key": "normal", "name": "Healthy, high confidence", "do": "Normal operation"},
    {"key": "cautious", "name": "Healthy, low confidence", "do": "Trade smaller, or just watch"},
    {"key": "late", "name": "Data or answer late", "do": "Hold, no new orders"},
    {"key": "fallback", "name": "Jev unavailable", "do": "Simple code-only rules"},
    {"key": "kill", "name": "Hard limit breached", "do": "Kill switch, close positions, alert"},
]

LOG_KEEP = 200
SERIES_STEP_S = 900   # chart: one point per 15 min
SERIES_KEEP_S = 2 * 86_400


class Desk:
    def __init__(self, market: MarketStore, state_path: str = "state/jev_desk.json", runner: str = "local",
                 log_dir: str = "logs", cfg: StateConfig | None = None, starting_equity: float = 10_000.0):
        self.cfg = cfg or StateConfig()
        self.market, self.state_path, self.runner = market, state_path, runner
        self.engine = FxStateEngine(market, self.cfg, starting_equity)
        self.snaplog = SnapshotLog(log_dir)
        self.lock = threading.Lock()
        self.started = self.created = time.time()
        self.rounds = self.rounds_before = 0
        self.errors = 0
        self.latest: dict[str, dict] = {}
        self.round_ms: deque[float] = deque(maxlen=500)
        self.log: deque[list] = deque(maxlen=LOG_KEEP)
        self.last_round_ts: float | None = None
        self._restore()
        self.event(f"Desk started ({runner}). Decides every {self.cfg.decide_every_min} min on closed 5-min bars. "
                   "Waiting for prices from the MT5 bridge.")

    # ---- persistence ----------------------------------------------------
    def _restore(self) -> None:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (OSError, ValueError):
            return
        if old.get("desk", {}).get("version") != 2:
            return  # older order-book desk state: start fresh
        self.created = old.get("created", self.started)
        self.rounds_before = old.get("totals", {}).get("rounds", 0)
        self.log.extend(old.get("log", [])[-LOG_KEEP:])

    def save(self) -> str:
        data = self.state(full=True)
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
        os.replace(tmp, self.state_path)
        return self.state_path

    def event(self, text: str) -> None:
        with self.lock:
            self.log.append([round(time.time(), 1), text])

    # ---- schedule -------------------------------------------------------
    def next_round(self, now: float) -> float:
        step = self.cfg.decide_every_min * 60
        return now - now % step + step + self.cfg.grace_s

    # ---- one decision round ---------------------------------------------
    def decide_round(self, t: float) -> dict[str, dict]:
        t0 = time.perf_counter()
        out, stale = {}, []
        for sym in INSTRUMENTS:
            try:
                snap = self.engine.snapshot(sym, t)
            except SnapshotTooLarge as e:
                with self.lock:
                    self.errors += 1
                self.event(str(e))
                continue
            except ValueError:
                out[sym] = {"t": t, "fresh": False, "reason": "no prices yet", "snap": None, "tokens": 0}
                stale.append(sym)
                continue
            fresh, reason = FxStateEngine.is_fresh(snap, self.cfg)
            rec = {"t": t, "fresh": fresh, "reason": reason, "snap": snap, "tokens": estimate_tokens(snap)}
            rec["decision"] = self._decide(sym, snap) if fresh else None
            if not fresh:
                stale.append(sym)
            self.snaplog.write(t, {"sym": sym, "snap": snap, "fresh": fresh, "model": None, "decision": rec["decision"]})
            out[sym] = rec
        self.snaplog.flush()
        ms = (time.perf_counter() - t0) * 1000
        with self.lock:
            self.latest.update(out)
            self.rounds += 1
            self.last_round_ts = t
            self.round_ms.append(ms)
        if stale and len(stale) == len(INSTRUMENTS):
            reason = next(iter(out.values()))["reason"] if out else "no data"
            if self.rounds == 1 or self._last_stale_reason != reason:
                self.event(f"No fresh prices ({reason}). Holding; nothing is decided on stale data.")
            self._last_stale_reason = reason
        else:
            if getattr(self, "_last_stale_reason", None):
                self.event("Fresh prices again.")
            self._last_stale_reason = None
            if stale:
                self.event("Stale, skipped this round: " + ", ".join(stale))
        return out

    _last_stale_reason: str | None = None

    def _decide(self, symbol: str, snap: dict):
        """Stages 2-5 go here: Jev battery -> policy -> risk veto.
        Stage 1: observe only, no orders."""
        return None

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            t = self.next_round(time.time())
            if stop.wait(max(0.0, t - time.time())):
                break
            self.decide_round(time.time())
        self.snaplog.close()

    # ---- what the dashboard reads ---------------------------------------
    def bridge_state(self, now: float) -> dict:
        m = self.market
        with m.lock:
            age = None if m.last_ingest is None else now - m.last_ingest
            b = dict(m.bridge)
            rej = m.rejected
        status = "waiting" if age is None else "live" if age < 60 else "down"
        return {"status": status, "age_s": None if age is None else round(age, 1), "rejected": rej, **b}

    def instruments(self, now: float, full: bool) -> dict:
        out = {}
        with self.market.lock:
            for sym, spec in INSTRUMENTS.items():
                sd = self.market.data[sym]
                q = sd.quotes[-1] if sd.quotes else None
                row = {"kind": spec["kind"], "pip": spec["pip"], "digits": spec["digits"],
                       "broker_symbol": sd.broker_symbol, "bars": len(sd.bars),
                       "quote": {"bid": q.bid, "ask": q.ask, "t": q.ts} if q else None}
                if full:
                    pts, last = [], -1e18
                    for b in sd.bars_closed_by(now):
                        if b.close_ts >= now - SERIES_KEEP_S and b.close_ts - last >= SERIES_STEP_S:
                            pts.append([b.close_ts, b.close])
                            last = b.close_ts
                    row["series"] = pts
                out[sym] = row
        with self.lock:
            for sym, rec in self.latest.items():
                out[sym]["last"] = rec
        return out

    def state(self, full: bool = False) -> dict:
        now = time.time()
        rm = list(self.round_ms)
        sess, sess_min, sess_name = session_at(now)
        with self.lock:
            toks = [r["tokens"] for r in self.latest.values() if r.get("tokens")]
            d = {
                "desk": {"name": "Jev Desk", "stage": 1, "model": MODEL_VERSION, "version": 2,
                         "market": "Gold + forex majors · MT5"},
                "runner": self.runner,
                "server_time": now,
                "created": self.created,
                "started": self.started,
                "cadence_min": self.cfg.decide_every_min,
                "next_round": self.next_round(now),
                "last_round": self.last_round_ts,
                "session": {"code": sess, "name": sess_name, "min": sess_min},
                "totals": {"rounds": self.rounds + self.rounds_before, "rounds_session": self.rounds, "errors": self.errors},
                "engine": {"round_ms_p50": round(statistics.median(rm), 1) if rm else None,
                           "tokens_max": max(toks) if toks else 0, "budget": self.cfg.max_snapshot_tokens},
                "account": {"start": self.engine.starting_equity, "equity": self.engine.starting_equity,
                            "realized": 0.0, "unrealized": 0.0, "open_positions": 0, "drawdown": 0.0},
                "stages": STAGES,
                "jev": {"status": "pending", "model": MODEL_VERSION, "questions": QUESTIONS},
                "policy": {"status": "pending"},
                "risk": {"status": "pending", "limits": [{"name": n, "value": None, "limit": None} for n in RISK_LIMITS]},
                "ladder": {"status": "pending", "rungs": LADDER, "current": None},
                "log": list(self.log) if full else list(self.log)[-60:],
            }
        d["bridge"] = self.bridge_state(now)
        d["instruments"] = self.instruments(now, full)
        return d
