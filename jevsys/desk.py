"""The desk: the decision loop for gold + forex and everything the dashboard shows.

Every `decide_every_min` minutes (15 by default), just after a 5-minute bar
closes, it builds a snapshot per instrument (Stage 1), then asks Jev the
six-question battery for every fresh one, in parallel (Stage 2). Snapshot,
answers and the model version are logged together for the calibration check.
Stages 3-5 plug into `_decide()` (policy -> risk veto). No orders exist yet.
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
from .jev import MODEL_VERSION, JevLayer, dashboard_questions
from .market import MarketStore
from .snapshot_log import SnapshotLog

STAGES = [
    {"n": 1, "name": "State engine", "what": "Snapshot per instrument, < 400 tokens, no future data", "status": "live"},
    {"n": 2, "name": "Jev decision layer", "what": "6-question battery per instrument, pinned version", "status": "live"},
    {"n": 3, "name": "Policy engine", "what": "Long / short / exit / stand aside by confidence", "status": "pending"},
    {"n": 4, "name": "Risk engine", "what": "Hard limits, stop per trade, veto on every order", "status": "pending"},
    {"n": 5, "name": "Loop + fallback ladder", "what": "Stale = hold, Jev down = code rules, limit = kill", "status": "pending"},
    {"n": 6, "name": "Paper trading + calibration", "what": "Two weeks before any real money", "status": "pending"},
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
                 log_dir: str = "logs", cfg: StateConfig | None = None, starting_equity: float = 10_000.0,
                 jev: JevLayer | None = None):
        self.cfg = cfg or StateConfig()
        self.jev = jev if jev is not None else JevLayer()
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
        self.event(f"Jev {self.jev.model} connected (direct TypeSafe API)." if self.jev.enabled
                   else f"Jev is off: {self.jev.reason}. Snapshots are still built and logged.")

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
            out[sym] = {"t": t, "fresh": fresh, "reason": reason, "snap": snap, "tokens": estimate_tokens(snap)}
            if not fresh:
                stale.append(sym)
        # Stage 2: one parallel Jev round for every fresh snapshot (stale ones are never asked)
        fresh_snaps = {s: r["snap"] for s, r in out.items() if r["fresh"]}
        answers = self.jev.ask_all(fresh_snaps)
        for sym, rec in out.items():
            j = answers.get(sym)
            rec["jev"] = j
            rec["decision"] = self._decide(sym, rec["snap"], j) if rec["fresh"] else None
            if rec["snap"] is not None:
                self.snaplog.write(t, {"sym": sym, "snap": rec["snap"], "fresh": rec["fresh"],
                                       "model": (j or {}).get("model"), "pinned": self.jev.model,
                                       "jev": (j or {}).get("answers"), "jev_ms": (j or {}).get("ms"),
                                       "jev_error": (j or {}).get("error"), "decision": rec["decision"]})
        self.snaplog.flush()
        self._jev_events(answers)
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

    def _jev_events(self, answers: dict[str, dict]) -> None:
        if not answers:
            return
        errs = {s: r for s, r in answers.items() if "error" in r}
        drift = {r["model"] for r in answers.values() if "error" not in r and not r.get("pinned_ok")}
        if drift:
            self.event(f"WARNING: TypeSafe answered with {', '.join(sorted(drift))}, not the pinned {self.jev.model}. "
                       "Thresholds are tuned to the pinned version.")
        kinds = sorted({r["kind"] for r in errs.values()})
        if errs and len(errs) == len(answers):
            if "auth" in kinds:
                self.event("Jev rejected the API key. Run server/jev-key.sh to set it again.")
            elif self._last_jev_problem != tuple(kinds):
                self.event(f"Jev unavailable this round ({', '.join(kinds)}): {next(iter(errs.values()))['error'][:120]}")
            self._last_jev_problem = tuple(kinds)
        else:
            if self._last_jev_problem:
                self.event("Jev answering again.")
            self._last_jev_problem = None
            if errs:
                self.event(f"Jev missed {len(errs)} of {len(answers)} ({', '.join(kinds)}): {', '.join(errs)}")
            ok = [r for r in answers.values() if "error" not in r]
            if ok and self._first_jev_ok:
                self._first_jev_ok = False
                ms = sorted(r["ms"] for r in ok)[len(ok) // 2]
                self.event(f"First Jev answers: {len(ok)} instruments, model {ok[0]['model']}, ~{ms} ms each.")

    _last_jev_problem: tuple | None = None
    _first_jev_ok = True

    def _decide(self, symbol: str, snap: dict, jev: dict | None):
        """Stages 3-5 go here: policy turns Jev's answers into an action, then the risk veto.
        Until then: observe only, no orders."""
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
                "desk": {"name": "Jev Desk", "stage": 2, "model": MODEL_VERSION, "version": 2,
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
                "stages": [dict(st, status="live" if self.jev.enabled else "needs key") if st["n"] == 2 else st
                           for st in STAGES],
                "jev": {**self.jev.status(), "questions": dashboard_questions()},
                "policy": {"status": "pending"},
                "risk": {"status": "pending", "limits": [{"name": n, "value": None, "limit": None} for n in RISK_LIMITS]},
                "ladder": {"status": "pending", "rungs": LADDER, "current": None},
                "log": list(self.log) if full else list(self.log)[-60:],
            }
        d["bridge"] = self.bridge_state(now)
        d["instruments"] = self.instruments(now, full)
        return d
