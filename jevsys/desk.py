"""The desk: the decision loop for gold + forex and everything the dashboard shows.

Every `decide_every_min` minutes (15 by default), just after a 5-minute bar closes:
  1. code builds a snapshot per instrument                        (Stage 1, state engine)
  2. Jev answers the six-question battery for every fresh one      (Stage 2, in parallel)
  3. the policy turns the answers into an action                   (Stage 3)
     - or, if Jev is down, the code-only rules do                  (Stage 5, fallback rung 4)
  4. the risk engine sizes it, sets the stop, and can veto it      (Stage 4)
  5. the paper account fills it at the live bid/ask
  + two shadow accounts trade the same data for Step 8: code-only rules, and Jev without the confidence gate
Between rounds a monitor checks every position on live quotes every 2 s, runs the kill
switches, watches the bridge, sends alerts, and refreshes the Step 8 report hourly.

Everything is logged together for the calibration check. Paper only: no real orders.
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time
from collections import deque

from . import report as step8
from .alerts import Alerter
from .config import StateConfig
from .fx_state import FxStateEngine, SnapshotTooLarge, estimate_tokens, session_at
from .instruments import INSTRUMENTS
from .jev import MODEL_VERSION, JevLayer, dashboard_questions
from .market import MarketStore
from .paper import PaperBook, exit_price, pnl_pips, pnl_usd
from .policy import Action, decide
from .risk import RiskEngine
from .rules import NO_GATE, decide_rules
from .settings import Settings
from .snapshot_log import SnapshotLog
from .trader import Trader
from .types import Quote

STAGES = [
    {"n": 1, "name": "State engine", "what": "Snapshot per instrument, < 400 tokens, no future data", "status": "live"},
    {"n": 2, "name": "Jev decision layer", "what": "6-question battery per instrument, pinned version", "status": "live"},
    {"n": 3, "name": "Policy engine", "what": "Long / short / exit / stand aside by confidence", "status": "live"},
    {"n": 4, "name": "Risk engine", "what": "Sizing, leverage, stops, vetoes, kill switch", "status": "live"},
    {"n": 5, "name": "Fallback ladder", "what": "Code-only rules when Jev is down, alerts, drilled", "status": "live"},
    {"n": 6, "name": "Paper trading + Step 8", "what": "Two weeks, then calibration, baselines, fees", "status": "running"},
]

LADDER = [
    {"key": "normal", "name": "Healthy, high confidence", "do": "Normal operation"},
    {"key": "cautious", "name": "Healthy, low confidence", "do": "Stand aside until Jev is sure"},
    {"key": "late", "name": "Data or answer late", "do": "Hold, no new orders"},
    {"key": "fallback", "name": "Jev unavailable", "do": "Simple code-only rules at reduced size"},
    {"key": "kill", "name": "Hard limit breached", "do": "Kill switch: everything closed, alert sent"},
]

LOG_KEEP = 300
SERIES_STEP_S = 900
SERIES_KEEP_S = 2 * 86_400
MONITOR_EVERY_S = 2.0
REPORT_EVERY_S = 3600


def market_open(t: float) -> bool:
    """Forex hours: Sunday 21:00 UTC to Friday 21:00 UTC."""
    g = time.gmtime(t)
    mins = g.tm_hour * 60 + g.tm_min
    return g.tm_wday in (0, 1, 2, 3) or (g.tm_wday == 4 and mins < 21 * 60) or (g.tm_wday == 6 and mins >= 21 * 60)


class Desk:
    def __init__(self, market: MarketStore, state_path: str = "state/jev_desk.json", runner: str = "local",
                 log_dir: str = "logs", cfg: StateConfig | None = None, starting_equity: float = 10_000.0,
                 jev: JevLayer | None = None, settings_path: str = "config/desk.json", book_path: str | None = None,
                 alerter: Alerter | None = None):
        self.cfg = cfg or StateConfig()
        self.market, self.state_path, self.runner, self.log_dir = market, state_path, runner, log_dir
        self.settings = Settings(settings_path)
        self.risk = RiskEngine(self.settings)
        sdir = os.path.dirname(state_path) or "."
        commission = self.settings.risk["commission_per_lot_usd"]
        self.alerter = alerter if alerter is not None else Alerter()
        self.lock = threading.Lock()
        self.log: deque[list] = deque(maxlen=LOG_KEEP)
        self.trader = Trader("jev", "Jev + confidence gate (live account)",
                             PaperBook(book_path or os.path.join(sdir, "paper_book.json"), starting_equity, commission),
                             self.risk, live=True, event=self.event, alert=self._alert)
        self.shadows = {
            "rules": Trader("rules", "Code-only rules (shadow)",
                            PaperBook(os.path.join(sdir, "shadow_rules.json"), starting_equity, commission), self.risk),
            "nogate": Trader("nogate", "Jev without confidence gate (shadow)",
                             PaperBook(os.path.join(sdir, "shadow_nogate.json"), starting_equity, commission), self.risk),
        }
        self.book = self.trader.book
        self.engine = FxStateEngine(market, self.cfg, starting_equity)
        self.engine.book = self.book
        self.jev = jev if jev is not None else JevLayer()
        self.snaplog = SnapshotLog(log_dir)
        self.trade_lock = threading.RLock()
        self.started = self.created = time.time()
        self.rounds = self.rounds_before = 0
        self.errors = 0
        self.latest: dict[str, dict] = {}
        self.round_ms: deque[float] = deque(maxlen=500)
        self.last_round_ts: float | None = None
        self.ladder_now: str | None = None
        self.fallback_on = False
        self.report: dict | None = None
        self._last_report = time.time() - REPORT_EVERY_S + 60   # first report a minute after start
        self._last_book_save = 0.0
        self._bridge_alerted = False
        self._summary_day = ""
        self.reset_flag = os.path.join(sdir, "RESET_KILL")
        self._restore()
        self.event(f"Desk started ({runner}). Decides every {self.cfg.decide_every_min} min on closed 5-min bars. "
                   "Waiting for prices from the MT5 bridge.")
        self.event(f"Jev {self.jev.model} connected (direct TypeSafe API)." if self.jev.enabled
                   else f"Jev is off: {self.jev.reason}." + (" Code-only rules will trade at reduced size."
                                                             if self.settings.fallback["enabled"] else ""))
        self.event(f"Settings version {self.settings.version}. Paper account: equity ${self.book.equity({}):,.2f}, "
                   f"{len(self.book.positions)} open position(s). Alerts: "
                   + ("Telegram on." if self.alerter.enabled else "off (run server/telegram.sh to turn them on).")
                   + (f" Kill switch ON: {self.book.halted['reason']}." if self.book.halted else ""))

    @property
    def traders(self) -> list[Trader]:
        return [self.trader, *self.shadows.values()]

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
        self.report = old.get("report")

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

    def _alert(self, text: str, key: str | None = None, cooldown_s: float = 0) -> None:
        self.alerter.send(text, key, cooldown_s)

    # ---- prices ------------------------------------------------------------
    def quotes(self, now: float, max_age: float | None = None) -> dict[str, Quote]:
        max_age = self.settings.risk["max_quote_age_s"] if max_age is None else max_age
        out = {}
        with self.market.lock:
            for s, sd in self.market.data.items():
                if sd.quotes and 0 <= now - sd.quotes[-1].ts <= max_age:
                    out[s] = sd.quotes[-1]
        return out

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

        answers = self.jev.ask_all({s: r["snap"] for s, r in out.items() if r["fresh"]})
        self._jev_events(answers)
        fb = self.settings.fallback
        # a late answer means hold (rung 3); only an unreachable or failing Jev means code-only rules (rung 4)
        late_round = any(a.get("kind") == "late" for a in answers.values())
        fallback_now = fb["enabled"] and not late_round and (
            not self.jev.enabled or self.jev.consec_errors >= fb["after_failed_rounds"])
        alerts = self.settings.alerts

        with self.trade_lock:
            self._check_reset()
            q_trade, q_any = self.quotes(t), self.quotes(t, 3600)
            for tr in self.traders:
                tr.mark(q_trade, t, q_any, alerts)
            used_rules = False
            for sym, rec in out.items():
                j = answers.get(sym)
                rec["jev"] = j
                if not rec["fresh"]:
                    rec["action"] = {"kind": "WAIT", "why": rec["reason"]}
                    rec["shadow"] = {}
                else:
                    rec["action"], rules_used = self._live_action(sym, rec, j, t, fallback_now, alerts)
                    used_rules |= rules_used
                    rec["shadow"] = self._shadow_actions(sym, rec, j, t)
                if rec["snap"] is not None:
                    jj = j or {}
                    self.snaplog.write(t, {"sym": sym, "snap": rec["snap"], "fresh": rec["fresh"], "model": jj.get("model"),
                                           "pinned": self.jev.model, "jev": jj.get("answers"), "jev_ms": jj.get("ms"),
                                           "jev_error": jj.get("error"), "action": rec["action"], "shadow": rec["shadow"],
                                           "settings": self.settings.version})
            self.snaplog.flush()
            self._fallback_events(fallback_now and any(r["fresh"] for r in out.values()), alerts)
            self.ladder_now = self._ladder(out, answers, used_rules or fallback_now)
            for tr in self.traders:
                tr.book.mark(self.quotes(t), t, force_point=True)

        ms = (time.perf_counter() - t0) * 1000
        with self.lock:
            self.latest.update(out)
            self.rounds += 1
            self.last_round_ts = t
            self.round_ms.append(ms)
        self._stale_events(out, stale)
        return out

    def _live_action(self, sym, rec, j, t, fallback_now, alerts) -> tuple[dict, bool]:
        pos = self.book.positions.get(sym)
        q = self.quotes(t).get(sym)
        snap = rec["snap"]
        common = dict(jev_consec_errors=self.jev.consec_errors, alerts=alerts, settings_version=self.settings.version)
        if j and "answers" in j:
            a = decide(j["answers"], pos.side if pos else None, self.settings.policy)
            return self.trader.act(sym, a, snap, q, t, source="jev", jev_rec=j, **common), False
        if fallback_now and (not j or j.get("kind") != "late"):
            a = decide_rules(snap, pos.side if pos else None, self.settings.fallback)
            return self.trader.act(sym, a, snap, q, t, source="rules", jev_rec=None,
                                   risk_mult=self.settings.fallback["risk_mult"], **common), True
        why = ("Jev answer late" if j and j.get("kind") == "late"
               else "Jev off" if not self.jev.enabled else f"no Jev answer ({(j or {}).get('kind', 'none')})")
        return {"kind": "HOLD" if pos else "WAIT", "why": why + ("; position stays on its stop" if pos else "; no new trade"),
                "source": "none"}, False

    def _shadow_actions(self, sym, rec, j, t) -> dict:
        snap, q = rec["snap"], self.quotes(t).get(sym)
        out = {}
        rt = self.shadows["rules"]
        p = rt.book.positions.get(sym)
        a = decide_rules(snap, p.side if p else None, self.settings.fallback)
        out["rules"] = rt.act(sym, a, snap, q, t, source="rules", jev_rec=None, jev_consec_errors=0,
                              settings_version=self.settings.version)["kind"]
        if j and "answers" in j:
            ng = self.shadows["nogate"]
            p = ng.book.positions.get(sym)
            a = decide(j["answers"], p.side if p else None, NO_GATE)
            out["nogate"] = ng.act(sym, a, snap, q, t, source="jev", jev_rec=j, jev_consec_errors=0,
                                   settings_version=self.settings.version)["kind"]
        return out

    def _ladder(self, out: dict, answers: dict, rules_active: bool) -> str:
        if self.book.halted:
            return "kill"
        fresh = [r for r in out.values() if r["fresh"]]
        if not fresh:
            return "late"
        if any(a.get("kind") == "late" for a in answers.values()):
            return "late"
        if rules_active or not self.jev.enabled or (answers and all("error" in a for a in answers.values())):
            return "fallback"
        return "normal" if any((r.get("action") or {}).get("confident") for r in fresh) else "cautious"

    # ---- between rounds ----------------------------------------------------
    def _check_reset(self) -> None:
        if os.path.exists(self.reset_flag):
            try:
                os.remove(self.reset_flag)
            except OSError:
                pass
            q = self.quotes(time.time(), 3600)
            for tr in self.traders:
                if tr.book.halted:
                    tr.book.reset_halt()
                    tr.book.peak = tr.book.equity(q)
            self.event("Kill switch reset by hand. Peak equity restarted from current equity.")
            self._alert("✅ Jev Desk kill switch reset by hand. Trading is back on.")

    def monitor_tick(self, now: float) -> None:
        alerts = self.settings.alerts
        with self.trade_lock:
            self._check_reset()
            q, q_any = self.quotes(now), self.quotes(now, 3600)
            for tr in self.traders:
                tr.check_exits(q, now, alerts if tr.live else None)
                tr.mark(q, now, q_any, alerts if tr.live else None)
            if now - self._last_book_save >= 60:
                for tr in self.traders:
                    tr.book.save()
                self._last_book_save = now
        self._watch_bridge(now, alerts)
        self._daily_summary(now, alerts)
        if now - self._last_report >= REPORT_EVERY_S:
            self._last_report = now
            self.refresh_report(now)

    def refresh_report(self, now: float | None = None) -> dict | None:
        try:
            self.report = step8.build(self.log_dir, self.traders, self.created, now)
        except Exception as e:  # a report problem must never stop trading
            self.event(f"Step 8 report failed: {e}")
        return self.report

    def _watch_bridge(self, now: float, alerts: dict) -> None:
        with self.market.lock:
            last = self.market.last_ingest
        silent = None if last is None else now - last
        limit = alerts["bridge_down_min"] * 60
        if silent is not None and silent > limit and market_open(now):
            if not self._bridge_alerted:
                self._bridge_alerted = True
                self.event(f"MT5 bridge silent for {silent / 60:.0f} min while the market is open.")
                self._alert(f"⚠️ Jev Desk: no prices from the MT5 bridge for {silent / 60:.0f} min. "
                            "Check the Windows PC is on and MetaTrader 5 is open. Open positions stay on their stops.")
        elif self._bridge_alerted and silent is not None and silent < 60:
            self._bridge_alerted = False
            self.event("MT5 bridge back.")
            self._alert("✅ Jev Desk: MT5 bridge is sending prices again.")

    def _daily_summary(self, now: float, alerts: dict) -> None:
        g = time.gmtime(now)
        day = time.strftime("%Y-%m-%d", g)
        if g.tm_wday > 4 or day == self._summary_day or g.tm_hour * 60 + g.tm_min < self.settings.minutes("daily_summary_utc", "alerts"):
            return
        self._summary_day = day
        bv = self.book_view(now, False)
        lines = [f"📊 Jev Desk daily · {day}",
                 f"Paper equity ${bv['equity']:,.2f} ({bv['today_pnl']:+,.2f} today, {bv['today_trades']} trades closed)",
                 f"Open positions: {len(bv['positions'])} · drawdown {bv['drawdown_pct']:.2f}%"]
        for tr in self.shadows.values():
            lines.append(f"{tr.label}: ${tr.book.equity(self.quotes(now, 3600)):,.2f}")
        js = self.jev.status()
        lines.append(f"Jev: {js['status']}, {js['calls']} answers, {js['errors']} errors · rung: {self.ladder_now or '–'}")
        self.event("Daily summary sent." if self.alerter.enabled else "Daily summary (alerts off).")
        self._alert("\n".join(lines))

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
    def _fallback_events(self, on: bool, alerts: dict) -> None:
        if on and not self.fallback_on:
            self.event("Jev unavailable: code-only rules are trading at reduced size (fallback rung 4).")
            if alerts.get("jev_down"):
                self._alert(f"⚠️ Jev Desk: Jev unavailable ({self.jev.reason or self.jev.last_error or 'errors'}). "
                            "Switched to code-only rules at reduced size.")
        elif not on and self.fallback_on:
            self.event("Jev back: code-only rules off.")
            if alerts.get("jev_down"):
                self._alert("✅ Jev Desk: Jev is answering again. Code-only rules off.")
        self.fallback_on = on

    def _jev_events(self, answers: dict[str, dict]) -> None:
        if not answers:
            return
        errs = {s: r for s, r in answers.items() if "error" in r}
        drift = {r["model"] for r in answers.values() if "error" not in r and not r.get("pinned_ok")}
        if drift:
            self.event(f"WARNING: TypeSafe answered with {', '.join(sorted(drift))}, not the pinned {self.jev.model}. "
                       "Thresholds are tuned to the pinned version.")
            self._alert(f"⚠️ Jev Desk: TypeSafe answered with {', '.join(sorted(drift))}, not the pinned {self.jev.model}.",
                        key="drift", cooldown_s=86_400)
        kinds = sorted({r["kind"] for r in errs.values()})
        if errs and len(errs) == len(answers):
            if "auth" in kinds:
                self.event("Jev rejected the API key. Run server/jev-key.sh to set it again.")
                self._alert("⚠️ Jev Desk: TypeSafe rejected the API key. Run server/jev-key.sh.", key="auth", cooldown_s=21_600)
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

    def book_view(self, now: float, full: bool, trader: Trader | None = None) -> dict:
        tr = trader or self.trader
        book = tr.book
        q = self.quotes(now, 3600)
        with book.lock:
            pos = []
            for sym, p in book.positions.items():
                px = exit_price(p.side, q[sym]) if sym in q else None
                pos.append({"id": p.id, "sym": sym, "side": p.side, "lots": p.lots, "entry": p.entry, "stop": p.stop,
                            "initial_stop": p.initial_stop, "target": p.target, "opened": p.opened, "price": px,
                            "pips": round(pnl_pips(p, px), 1) if px else None,
                            "pnl": round(pnl_usd(p, px), 2) if px else None, "risk_usd": p.risk_usd, "reason": p.reason})
            eq = book.equity(q)
            day = time.strftime("%Y-%m-%d", time.gmtime(now))
            today = [x for x in book.trades if time.strftime("%Y-%m-%d", time.gmtime(x["closed"])) == day]
            return {"start": book.start_equity, "equity": round(eq, 2), "realized": round(book.realized, 2),
                    "unrealized": round(eq - book.start_equity - book.realized, 2),
                    "peak": round(book.peak, 2), "day_start": round(book.day_start, 2),
                    "today_pnl": round(eq - book.day_start, 2), "today_trades": len(today),
                    "drawdown_pct": round(tr.last_mark.get("drawdown_pct", 0.0), 3),
                    "halted": book.halted, "positions": pos, "stats": book.stats(),
                    "trades": book.trades[-(200 if full else 30):][::-1],
                    "equity_pts": book.equity_pts[-2016:] if full else None}

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
        shadows = {}
        for name, tr in self.shadows.items():
            bv = self.book_view(now, False, tr)
            shadows[name] = {"label": tr.label, "equity": bv["equity"], "today_pnl": bv["today_pnl"],
                             "open": len(bv["positions"]), "stats": bv["stats"], "halted": bv["halted"],
                             "drawdown_pct": bv["drawdown_pct"],
                             "equity_pts": tr.book.equity_pts[-2016:] if full else None}
        return {
            "desk": {"name": "Jev Desk", "stage": 5, "model": MODEL_VERSION, "version": 2,
                     "market": "Gold + forex majors · MT5"},
            "runner": self.runner,
            "server_time": now,
            "created": self.created,
            "started": self.started,
            "cadence_min": self.cfg.decide_every_min,
            "next_round": self.next_round(now),
            "last_round": self.last_round_ts,
            "session": {"code": sess, "name": sess_name, "min": sess_min},
            "market_open": market_open(now),
            "totals": {"rounds": self.rounds + self.rounds_before, "rounds_session": self.rounds, "errors": self.errors},
            "engine": {"round_ms_p50": round(statistics.median(rm), 1) if rm else None,
                       "tokens_max": max(toks) if toks else 0, "budget": self.cfg.max_snapshot_tokens},
            "book": book,
            "shadows": shadows,
            "account": {"start": book["start"], "equity": book["equity"], "realized": book["realized"],
                        "unrealized": book["unrealized"], "open_positions": len(book["positions"]),
                        "drawdown": book["drawdown_pct"] / 100},
            "stages": stages,
            "jev": {**jst, "questions": dashboard_questions()},
            "policy": {"status": "live", "settings_version": self.settings.version, "settings_error": self.settings.error,
                       "thresholds": self.settings.policy, "fallback": self.settings.fallback},
            "risk": {"status": "live", "settings": self.settings.risk,
                     "limits": self.risk.limits_view(self.book, {**self.trader.last_mark, "equity": book["equity"]},
                                                     jst["consec_errors"], max(svn) if svn else None,
                                                     round(max(ages), 1) if ages else None, max(jms) if jms else None)},
            "ladder": {"status": "live", "rungs": LADDER, "current": self.ladder_now, "fallback_on": self.fallback_on},
            "alerts": self.alerter.status(),
            "report": self.report,
            "bridge": self.bridge_state(now),
            "instruments": self.instruments(now, full),
            "log": log,
        }
