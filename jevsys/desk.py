"""The desk: the decision loop for gold + forex and everything the dashboard shows.

Every `decide_every_min` minutes (15 by default), just after a 5-minute bar closes:
  1. code builds a snapshot per instrument                        (Stage 1, state engine)
  2. Jev answers the six-question battery for every fresh one      (Stage 2, in parallel)
  3. the policy turns the answers into an action                   (Stage 3)
  4. the risk engine sizes it, sets the stop, and can veto it      (Stage 4)
  5. the paper book fills it at the live bid/ask
Between rounds a monitor checks every open position on live quotes every 2 s
(stop, target, max hold, Friday close) and runs the kill switches.

Snapshot, Jev's answers, model version, action, vetoes and settings version are
logged together for the Step 8 calibration check. Paper only: no real orders.
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
from .paper import PaperBook, exit_price, pnl_pips, pnl_usd
from .policy import Action, decide, tightened_stop
from .risk import RiskEngine
from .settings import Settings
from .snapshot_log import SnapshotLog
from .types import Quote

STAGES = [
    {"n": 1, "name": "State engine", "what": "Snapshot per instrument, < 400 tokens, no future data", "status": "live"},
    {"n": 2, "name": "Jev decision layer", "what": "6-question battery per instrument, pinned version", "status": "live"},
    {"n": 3, "name": "Policy engine", "what": "Long / short / exit / stand aside by confidence", "status": "live"},
    {"n": 4, "name": "Risk engine", "what": "Sizing, stop on every trade, vetoes, kill switch", "status": "live"},
    {"n": 5, "name": "Fallback ladder", "what": "Code-only rules when Jev is down; tested rungs", "status": "pending"},
    {"n": 6, "name": "Paper trading + calibration", "what": "Two weeks, then the Step 8 checks", "status": "pending"},
]

LADDER = [
    {"key": "normal", "name": "Healthy, high confidence", "do": "Normal operation"},
    {"key": "cautious", "name": "Healthy, low confidence", "do": "Stand aside until Jev is sure"},
    {"key": "late", "name": "Data or answer late", "do": "Hold, no new orders"},
    {"key": "fallback", "name": "Jev unavailable", "do": "Hold open positions on their stops (code-only rules: Stage 5)"},
    {"key": "kill", "name": "Hard limit breached", "do": "Kill switch: everything closed, no new trades"},
]

LOG_KEEP = 300
SERIES_STEP_S = 900
SERIES_KEEP_S = 2 * 86_400
MONITOR_EVERY_S = 2.0


class Desk:
    def __init__(self, market: MarketStore, state_path: str = "state/jev_desk.json", runner: str = "local",
                 log_dir: str = "logs", cfg: StateConfig | None = None, starting_equity: float = 10_000.0,
                 jev: JevLayer | None = None, settings_path: str = "config/desk.json", book_path: str | None = None):
        self.cfg = cfg or StateConfig()
        self.market, self.state_path, self.runner = market, state_path, runner
        self.settings = Settings(settings_path)
        self.risk = RiskEngine(self.settings)
        self.book = PaperBook(book_path or os.path.join(os.path.dirname(state_path) or ".", "paper_book.json"),
                              starting_equity, self.settings.risk["commission_per_lot_usd"])
        self.engine = FxStateEngine(market, self.cfg, starting_equity)
        self.engine.book = self.book
        self.jev = jev if jev is not None else JevLayer()
        self.snaplog = SnapshotLog(log_dir)
        self.trade_lock = threading.RLock()   # one trade operation at a time (round vs monitor)
        self.lock = threading.Lock()
        self.started = self.created = time.time()
        self.rounds = self.rounds_before = 0
        self.errors = 0
        self.latest: dict[str, dict] = {}
        self.round_ms: deque[float] = deque(maxlen=500)
        self.log: deque[list] = deque(maxlen=LOG_KEEP)
        self.last_round_ts: float | None = None
        self.last_mark: dict = {"equity": starting_equity, "drawdown_pct": 0.0, "daily_loss_pct": 0.0}
        self.ladder_now: str | None = None
        self._last_veto: dict[str, tuple] = {}
        self.reset_flag = os.path.join(os.path.dirname(state_path) or ".", "RESET_KILL")
        self._restore()
        self.event(f"Desk started ({runner}). Decides every {self.cfg.decide_every_min} min on closed 5-min bars. "
                   "Waiting for prices from the MT5 bridge.")
        self.event(f"Jev {self.jev.model} connected (direct TypeSafe API)." if self.jev.enabled
                   else f"Jev is off: {self.jev.reason}. Snapshots are still built and logged.")
        self.event(f"Settings version {self.settings.version}. Paper book: equity ${self.book.equity({}):,.2f}, "
                   f"{len(self.book.positions)} open position(s)." + (f" Kill switch ON: {self.book.halted['reason']}." if self.book.halted else ""))

    # ---- persistence ----------------------------------------------------
    def _restore(self) -> None:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (OSError, ValueError):
            return
        if old.get("desk", {}).get("version") != 2:
            return
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

    # ---- prices ------------------------------------------------------------
    def quotes(self, now: float, max_age: float | None = None) -> dict[str, Quote]:
        """Latest quote per instrument, only if fresh enough to trade on."""
        max_age = self.settings.risk["max_quote_age_s"] if max_age is None else max_age
        out = {}
        with self.market.lock:
            for s, sd in self.market.data.items():
                if sd.quotes and 0 <= now - sd.quotes[-1].ts <= max_age:
                    out[s] = sd.quotes[-1]
        return out

    # ---- schedule -------------------------------------------------------
    def next_round(self, now: float) -> float:
        step = self.cfg.decide_every_min * 60
        return now - now % step + step + self.cfg.grace_s

    # ---- one decision round ---------------------------------------------
    def decide_round(self, t: float) -> dict[str, dict]:
        t0 = time.perf_counter()
        msg = self.settings.reload()
        if msg:
            self.event(msg)
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
        answers = self.jev.ask_all({s: r["snap"] for s, r in out.items() if r["fresh"]})
        self._jev_events(answers)

        # Stages 3-4: policy, risk, paper fills, under the trade lock
        with self.trade_lock:
            self._check_reset()
            m = self._mark(t)
            for sym, rec in out.items():
                rec["jev"] = answers.get(sym)
                rec["action"] = self._act(sym, rec, t, m) if rec["fresh"] else {"kind": "WAIT", "why": rec["reason"]}
                if rec["snap"] is not None:
                    j = rec["jev"] or {}
                    self.snaplog.write(t, {"sym": sym, "snap": rec["snap"], "fresh": rec["fresh"], "model": j.get("model"),
                                           "pinned": self.jev.model, "jev": j.get("answers"), "jev_ms": j.get("ms"),
                                           "jev_error": j.get("error"), "action": rec["action"],
                                           "settings": self.settings.version})
            self.snaplog.flush()
            self.ladder_now = self._ladder(out, answers)
            self.book.mark(self.quotes(t), t, force_point=True)

        ms = (time.perf_counter() - t0) * 1000
        with self.lock:
            self.latest.update(out)
            self.rounds += 1
            self.last_round_ts = t
            self.round_ms.append(ms)
        self._stale_events(out, stale)
        return out

    def _act(self, sym: str, rec: dict, t: float, m: dict) -> dict:
        j = rec["jev"]
        if not j or "answers" not in j:
            pos = self.book.positions.get(sym)
            why = "Jev off" if not self.jev.enabled else f"no Jev answer ({(j or {}).get('kind', 'none')})"
            return {"kind": "HOLD" if pos else "WAIT", "why": why + ("; position stays on its stop" if pos else "; no new trade")}
        pos = self.book.positions.get(sym)
        a: Action = decide(j["answers"], pos.side if pos else None, self.settings.policy)
        d = a.as_dict()
        q = self.quotes(t).get(sym)
        if a.kind == "ENTER":
            plan = self.risk.plan(sym, a.side, q, rec["snap"].get("atr_pips"), m["equity"]) if q else None
            vetoes = self.risk.entry_vetoes(sym, a.side, rec["snap"], q, t, self.book, j, self.jev.consec_errors, plan)
            d["plan"] = plan
            if vetoes:
                d.update(kind="VETOED", vetoes=vetoes)
                key = (a.side, tuple(vetoes))
                if self._last_veto.get(sym) != key:   # log a veto when it changes, not every round
                    self.event(f"{sym} {a.side} vetoed by risk: {'; '.join(vetoes)}")
                self._last_veto[sym] = key
                return d
            self._last_veto.pop(sym, None)
            p = self.book.open(sym, a.side, plan["lots"], q, plan["stop"], plan["target"], t, a.why,
                               jev=j["answers"], settings=self.settings.version)
            d["position_id"] = p.id
            self.event(f"OPEN {sym} {a.side} {p.lots:.2f} lots @ {p.entry} · stop {p.stop} · target {p.target} "
                       f"· risk ${p.risk_usd:,.2f} · {a.why}")
        elif a.kind == "EXIT" and pos and q:
            tr = self.book.close(sym, q, t, a.why)
            d["trade"] = tr["id"]
            self.event(f"CLOSE {sym} {tr['side']} @ {tr['exit']} · {tr['pips']:+.1f} pips · ${tr['pnl']:+,.2f} · {a.why}")
        elif a.kind == "TIGHTEN" and pos and q:
            px = exit_price(pos.side, q)
            old_stop = pos.stop
            new = tightened_stop(pos.side, pos.entry, pos.stop, px, INSTRUMENTS[sym]["digits"])
            if self.book.move_stop(sym, new):
                d["new_stop"] = new
                self.event(f"TIGHTEN {sym} stop {old_stop} -> {new} · {a.why}")
            else:
                d.update(kind="HOLD", why=a.why + " (stop already tighter)")
        return d

    def _ladder(self, out: dict, answers: dict) -> str:
        if self.book.halted:
            return "kill"
        fresh = [r for r in out.values() if r["fresh"]]
        if not fresh:
            return "late"
        if not self.jev.enabled or (answers and all("error" in a for a in answers.values())):
            return "fallback"
        if any(a.get("kind") == "late" for a in answers.values()):
            return "late"
        return "normal" if any((r.get("action") or {}).get("confident") for r in fresh) else "cautious"

    # ---- between rounds: stops, targets, kill switches ---------------------
    def _mark(self, t: float) -> dict:
        m = self.book.mark(self.quotes(t), t)
        self.last_mark = m
        kill = self.risk.kill_check(m)
        if kill and not self.book.halted:
            kind, reason = kill
            closed = self._close_all(t, f"kill switch: {reason}")
            self.book.halt(kind, reason, t)
            self.event(f"KILL SWITCH ({kind}): {reason}. Closed {closed} position(s). "
                       + ("No new trades until 00:00 UTC." if kind == "daily" else "Trading stays off until reset by hand."))
        if m.get("new_day") and self.rounds:
            self.event(f"New trading day. Starting equity ${m['day_start']:,.2f}.")
        return m

    def _close_all(self, t: float, why: str) -> int:
        q = self.quotes(t, max_age=3600)  # close on the last known price even if it's a bit old
        n = 0
        for sym in list(self.book.positions):
            if sym in q:
                tr = self.book.close(sym, q[sym], t, why)
                self.event(f"CLOSE {sym} {tr['side']} @ {tr['exit']} · {tr['pips']:+.1f} pips · ${tr['pnl']:+,.2f} · {why}")
                n += 1
        return n

    def _check_reset(self) -> None:
        if os.path.exists(self.reset_flag):
            try:
                os.remove(self.reset_flag)
            except OSError:
                pass
            if self.book.halted:
                self.book.reset_halt()
                self.book.peak = self.book.equity(self.quotes(time.time(), 3600))
                self.event("Kill switch reset by hand. Peak equity restarted from current equity.")

    def monitor_tick(self, now: float) -> None:
        with self.trade_lock:
            self._check_reset()
            q = self.quotes(now)
            for sym, pos in list(self.book.positions.items()):
                if sym not in q:
                    continue
                why = self.risk.exit_reason(pos, q[sym], now)
                if why:
                    tr = self.book.close(sym, q[sym], now, why)
                    self.event(f"CLOSE {sym} {tr['side']} @ {tr['exit']} · {tr['pips']:+.1f} pips · ${tr['pnl']:+,.2f} · {why}")
            self._mark(now)
            if now - self._last_book_save >= 60:   # peak, day start and equity curve survive a restart
                self.book.save()
                self._last_book_save = now

    _last_book_save = 0.0

    def monitor(self, stop: threading.Event) -> None:
        while not stop.wait(MONITOR_EVERY_S):
            try:
                self.monitor_tick(time.time())
            except Exception as e:  # never let the stop checker die silently
                self.event(f"Monitor error: {e}")

    def run(self, stop: threading.Event) -> None:
        threading.Thread(target=self.monitor, args=(stop,), daemon=True, name="monitor").start()
        while not stop.is_set():
            t = self.next_round(time.time())
            if stop.wait(max(0.0, t - time.time())):
                break
            self.decide_round(time.time())
        self.snaplog.close()

    # ---- events --------------------------------------------------------------
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

    def _stale_events(self, out: dict, stale: list) -> None:
        if stale and len(stale) == len(INSTRUMENTS):
            reason = next(iter(out.values()))["reason"] if out else "no data"
            if self.rounds == 1 or self._last_stale_reason != reason:
                self.event(f"No fresh prices ({reason}). Holding; nothing is decided on stale data.")
            self._last_stale_reason = reason
        else:
            if self._last_stale_reason:
                self.event("Fresh prices again.")
            self._last_stale_reason = None
            if stale:
                self.event("Stale, skipped this round: " + ", ".join(stale))

    _last_jev_problem: tuple | None = None
    _first_jev_ok = True
    _last_stale_reason: str | None = None

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

    def book_view(self, now: float, full: bool) -> dict:
        q = self.quotes(now, 3600)
        with self.book.lock:
            pos = []
            for sym, p in self.book.positions.items():
                px = exit_price(p.side, q[sym]) if sym in q else None
                pos.append({"id": p.id, "sym": sym, "side": p.side, "lots": p.lots, "entry": p.entry, "stop": p.stop,
                            "initial_stop": p.initial_stop, "target": p.target, "opened": p.opened, "price": px,
                            "pips": round(pnl_pips(p, px), 1) if px else None,
                            "pnl": round(pnl_usd(p, px), 2) if px else None, "risk_usd": p.risk_usd, "reason": p.reason})
            eq = self.book.equity(q)
            day = time.strftime("%Y-%m-%d", time.gmtime(now))
            today = [t for t in self.book.trades if time.strftime("%Y-%m-%d", time.gmtime(t["closed"])) == day]
            return {"start": self.book.start_equity, "equity": round(eq, 2), "realized": round(self.book.realized, 2),
                    "unrealized": round(eq - self.book.start_equity - self.book.realized, 2),
                    "peak": round(self.book.peak, 2), "day_start": round(self.book.day_start, 2),
                    "today_pnl": round(eq - self.book.day_start, 2), "today_trades": len(today),
                    "drawdown_pct": round(self.last_mark.get("drawdown_pct", 0.0), 3),
                    "halted": self.book.halted, "positions": pos, "stats": self.book.stats(),
                    "trades": self.book.trades[-(200 if full else 30):][::-1],
                    "equity_pts": self.book.equity_pts[-2016:] if full else None}

    def state(self, full: bool = False) -> dict:
        now = time.time()
        rm = list(self.round_ms)
        sess, sess_min, sess_name = session_at(now)
        book = self.book_view(now, full)
        q = self.quotes(now, 10 ** 9)
        with self.lock:
            latest = dict(self.latest)
            toks = [r["tokens"] for r in latest.values() if r.get("tokens")]
            log = list(self.log) if full else list(self.log)[-80:]
        svn = [r["snap"].get("spread_vs_norm") for r in latest.values() if r.get("snap") and r["snap"].get("spread_vs_norm")]
        ages = [now - qq.ts for s, qq in q.items() if s in self.book.positions] or [now - qq.ts for qq in q.values()]
        jms = [(r.get("jev") or {}).get("ms") for r in latest.values() if (r.get("jev") or {}).get("ms")]
        jst = self.jev.status()
        stages = [dict(st, status="live" if self.jev.enabled else "needs key") if st["n"] == 2 else st for st in STAGES]
        return {
            "desk": {"name": "Jev Desk", "stage": 4, "model": MODEL_VERSION, "version": 2,
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
            "book": book,
            "account": {"start": book["start"], "equity": book["equity"], "realized": book["realized"],
                        "unrealized": book["unrealized"], "open_positions": len(book["positions"]),
                        "drawdown": book["drawdown_pct"] / 100},
            "stages": stages,
            "jev": {**jst, "questions": dashboard_questions()},
            "policy": {"status": "live", "settings_version": self.settings.version, "settings_error": self.settings.error,
                       "thresholds": self.settings.policy},
            "risk": {"status": "live", "settings": self.settings.risk,
                     "limits": self.risk.limits_view(self.book, {**self.last_mark, "equity": book["equity"]},
                                                     jst["consec_errors"], max(svn) if svn else None,
                                                     round(max(ages), 1) if ages else None, max(jms) if jms else None)},
            "ladder": {"status": "live", "rungs": LADDER, "current": self.ladder_now},
            "bridge": self.bridge_state(now),
            "instruments": self.instruments(now, full),
            "log": log,
        }
