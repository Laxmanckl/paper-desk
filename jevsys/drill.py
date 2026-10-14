"""Guide Step 6 / "When things go wrong": test each rung of the fallback ladder on purpose.

    python -m jevsys drill            all rungs, on a simulated market in a temporary folder
    python -m jevsys drill --send     ...and send one real Telegram test message

The drill never touches the live paper account, its logs or its settings file:
it builds a separate desk on simulated prices and breaks it deliberately.
  1  healthy, confident Jev        -> normal rung, a position opens with a stop
  2  healthy, unsure Jev           -> cautious rung, stands aside
  3  Jev answers late              -> late rung, holds, no new orders
  4  Jev unreachable (2 rounds)    -> fallback rung, code-only rules trade at reduced size
  5  prices stop (bridge down)     -> late rung, nothing decided on stale data
  6  daily loss limit breached     -> kill rung, everything closed, alert raised
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time

from .alerts import Alerter
from .desk import Desk
from .jev import JevLayer
from .market import MarketStore
from .sim_fx import SimBridge


def _answers(direction="up", dconf=0.85, qlevel=2, qconf=0.6):
    labels = ["Do not enter", "Marginal", "Good", "Excellent"]
    return {"regime": {"value": "trending", "p": 0.7}, "direction": {"value": direction, "p": dconf},
            "move_continues": {"p": 0.7}, "costs_abnormal": {"p": 0.1},
            "entry_quality": {"level": qlevel, "label": labels[qlevel], "p": qconf, "score": qlevel},
            "exit_urgency": {"level": 0, "label": "No urgency", "p": 0.7, "score": 0}}


class DrillJev(JevLayer):
    """Plays TypeSafe for the drill: answers, answers late, or is unreachable."""

    def __init__(self):
        self.model, self.reason, self.client = "jev-1.13.0", "", object()
        self.calls = self.errors = self.late = self.consec_errors = self.tokens_in = 0
        self.latencies, self.last_model_seen, self.last_error, self.lock = [], None, None, threading.Lock()
        self.mode, self.script = "ok", (lambda s: _answers("neutral"))

    def ask_all(self, snaps):
        if self.mode == "ok":
            self.consec_errors = 0
            return {s: {"answers": self.script(s), "model": self.model, "ms": 200, "tokens_in": 660, "pinned_ok": True}
                    for s in snaps}
        self.consec_errors += 1
        kind = "late" if self.mode == "late" else "network"
        self.last_error = f"{kind}: drill"
        return {s: {"error": "drill: " + ("no answer within 20s" if kind == "late" else "connection refused"),
                    "kind": kind, "ms": 20_000} for s in snaps}


def _last_wednesday_1500() -> float:
    now = time.time()
    t = now - now % 86_400 + 15 * 3600
    while time.gmtime(t).tm_wday != 2 or t > now:
        t -= 86_400
    return t


def run_drill(verbose: bool = True, send: bool = False) -> list[tuple[str, bool, str]]:
    tmp = tempfile.mkdtemp(prefix="jev-drill-")
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str):
        results.append((name, bool(ok), detail))
        if verbose:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    try:
        t = _last_wednesday_1500()
        sim = SimBridge(seed=3, start_ts=t - 3 * 86_400)
        m = MarketStore()
        m.ingest(sim.step_to(t), t)
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cfg = json.load(open(os.path.join(here, "config", "desk.json"), encoding="utf-8"))
        # loosen the code-only rules so the drill's simulated market always gives them something to do
        cfg["fallback"].update(min_ema20_dist_atr=0.0, min_ema20_slope_atr=0.0, rsi_long_min=0, rsi_long_max=100,
                               rsi_short_min=0, rsi_short_max=100, max_spread_vs_normal=10)
        cfg["risk"]["max_daily_loss_pct"] = 0.3
        json.dump(cfg, open(os.path.join(tmp, "desk.json"), "w"))
        sent = []
        jev = DrillJev()
        d = Desk(m, state_path=os.path.join(tmp, "state", "jev_desk.json"), log_dir=os.path.join(tmp, "logs"),
                 jev=jev, settings_path=os.path.join(tmp, "desk.json"), alerter=Alerter(send_fn=sent.append))

        jev.mode, jev.script = "ok", (lambda s: _answers("up") if s == "EURUSD" else _answers("neutral"))
        d.decide_round(t)
        p = d.book.positions.get("EURUSD")
        check("1 healthy + confident", d.ladder_now == "normal" and p is not None and p.stop != p.entry,
              f"rung={d.ladder_now}, EURUSD {'opened with stop ' + str(p.stop) if p else 'not opened'}")

        jev.script = lambda s: _answers("up", dconf=0.55, qlevel=1, qconf=0.3)
        n_before = len(d.book.positions)
        d.decide_round(t + 1)
        check("2 healthy + unsure", d.ladder_now == "cautious" and len(d.book.positions) == n_before,
              f"rung={d.ladder_now}, no new positions")

        jev.mode = "late"
        out = d.decide_round(t + 2)
        check("3 Jev late", d.ladder_now == "late" and all(r["action"]["kind"] in ("WAIT", "HOLD") for r in out.values()),
              f"rung={d.ladder_now}, every instrument held")

        jev.mode = "down"
        d.decide_round(t + 3)
        out = d.decide_round(t + 4)
        rules = [s for s, r in out.items() if r["action"].get("source") == "rules"]
        check("4 Jev unreachable", d.ladder_now == "fallback" and d.fallback_on and rules,
              f"rung={d.ladder_now}, code-only rules decided {len(rules)} instruments, "
              f"alert: {'yes' if any('code-only rules' in x for x in sent + [h[1] for h in d.alerter.history]) else 'no'}")

        jev.mode = "ok"
        out = d.decide_round(t + 3600)            # an hour later with no new prices
        check("5 prices stopped", d.ladder_now == "late" and not any(r["fresh"] for r in out.values()),
              f"rung={d.ladder_now}, nothing decided on stale data")

        t2 = t + 3601
        m.ingest(sim.step_to(t2), t2)
        for sym, pos in list(d.book.positions.items()):
            pip = 0.01 if "JPY" in sym else 0.1 if sym == "XAUUSD" else 0.0001
            px = pos.entry - pos.dir * 300 * pip     # 300 pips against every open position
            m.ingest({"symbols": {sym: {"quote": {"t": t2, "bid": px, "ask": px + pip}}}}, t2)
        had = len(d.book.positions)
        d.monitor_tick(t2)
        d.decide_round(t2 + 1)
        check("6 limit breached", d.ladder_now == "kill" and d.book.halted and not d.book.positions,
              f"rung={d.ladder_now}, closed {had} position(s), alert: "
              f"{'yes' if any('KILL SWITCH' in h[1] for h in d.alerter.history) else 'no'}")

        if send:
            real = Alerter()
            if not real.enabled:
                check("7 Telegram", False, "alerts not set up: run bash ~/paper-desk/server/telegram.sh")
            else:
                try:
                    real.send_now("🧪 Jev Desk drill: alerts reach your phone. All ladder rungs were tested.")
                    check("7 Telegram", True, "test message sent")
                except Exception as e:
                    check("7 Telegram", False, f"send failed: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return results
