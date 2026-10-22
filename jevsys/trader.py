"""One paper account plus the code that trades it: sizing, vetoes, fills, stops, kill switches.

The live desk account and the two Step 8 shadow accounts are all Traders, so
they trade identical data under identical risk rules. Only where their
decisions come from differs:
  jev     Jev + confidence gate (falls back to the code-only rules when Jev is down)   - live, alerts
  rules   code-only rules on every round                                                - shadow
  nogate  Jev without the confidence gate (acts on Jev's top answer at any confidence)  - shadow
  vbo     volume breakout on hourly bars (jevsys/vbo), the main account since 23 Oct     - live, alerts
"""
from __future__ import annotations

from .instruments import INSTRUMENTS
from .paper import PaperBook, exit_price
from .policy import Action, tightened_stop
from .risk import RiskEngine, notional_usd


class Trader:
    def __init__(self, name: str, label: str, book: PaperBook, risk: RiskEngine, live: bool = False,
                 event=None, alert=None, prefix: str = ""):
        self.name, self.label, self.book, self.risk, self.live = name, label, book, risk, live
        self.prefix = prefix  # put in front of this account's log lines and alerts
        self._event = event or (lambda text: None)
        self._alert = alert or (lambda text, key=None, cooldown_s=0: None)
        self.last_mark: dict = {"equity": book.start_equity, "drawdown_pct": 0.0, "daily_loss_pct": 0.0}
        self._last_veto: dict[str, tuple] = {}

    def event(self, text: str) -> None:
        if self.live:
            self._event(self.prefix + text)

    def alert(self, text: str, key: str | None = None, cooldown_s: float = 0) -> None:
        if self.live:
            self._alert(self.prefix + text, key, cooldown_s)

    def notional_open(self) -> float:
        return sum(notional_usd(s, p.lots, p.entry) for s, p in self.book.positions.items())

    # ---- one decision ------------------------------------------------------
    def act(self, sym: str, a: Action, snap: dict, q, t: float, *, source: str, jev_rec: dict | None,
            jev_consec_errors: int, risk_mult: float = 1.0, alerts: dict | None = None, settings_version: str = "") -> dict:
        d = a.as_dict()
        d["source"] = source
        pos = self.book.positions.get(sym)
        tag = "" if source == "jev" else " [code-only rules]"
        if a.kind == "ENTER":
            plan = (self.risk.plan(sym, a.side, q, snap.get("atr_pips"), self.last_mark["equity"],
                                   self.notional_open(), risk_mult) if q else None)
            vetoes = self.risk.entry_vetoes(sym, a.side, snap, q, t, self.book, jev_rec, jev_consec_errors, plan, source)
            d["plan"] = plan
            if vetoes:
                d.update(kind="VETOED", vetoes=vetoes)
                key = (a.side, tuple(vetoes))
                if self._last_veto.get(sym) != key:   # a veto is logged when it changes, not every round
                    self.event(f"{sym} {a.side} vetoed by risk: {'; '.join(vetoes)}{tag}")
                self._last_veto[sym] = key
                return d
            self._last_veto.pop(sym, None)
            p = self.book.open(sym, a.side, plan["lots"], q, plan["stop"], plan["target"], t, a.why + tag,
                               jev=(jev_rec or {}).get("answers") or {}, settings=settings_version)
            d["position_id"] = p.id
            msg = (f"OPEN {sym} {a.side} {p.lots:.2f} lots @ {p.entry} · stop {p.stop} · target {p.target} "
                   f"· risk ${p.risk_usd:,.2f} · {a.why}{tag}")
            self.event(msg)
            if (alerts or {}).get("trades"):
                self.alert(f"📈 Paper {msg}")
        elif a.kind == "EXIT" and pos and q:
            tr = self.close(sym, q, t, a.why, alerts)
            d["trade"] = tr["id"]
        elif a.kind == "TIGHTEN" and pos and q:
            old = pos.stop
            new = tightened_stop(pos.side, pos.entry, pos.stop, exit_price(pos.side, q), INSTRUMENTS[sym]["digits"])
            if self.book.move_stop(sym, new):
                d["new_stop"] = new
                self.event(f"TIGHTEN {sym} stop {old} -> {new} · {a.why}")
            else:
                d.update(kind="HOLD", why=a.why + " (stop already tighter)")
        return d

    def close(self, sym: str, q, t: float, why: str, alerts: dict | None = None) -> dict:
        tr = self.book.close(sym, q, t, why)
        msg = f"CLOSE {sym} {tr['side']} @ {tr['exit']} · {tr['pips']:+.1f} pips · ${tr['pnl']:+,.2f} · {why}"
        self.event(msg)
        if (alerts or {}).get("trades"):
            self.alert(("✅" if tr["pnl"] > 0 else "🔻") + f" Paper {msg}")
        return tr

    # ---- between decisions ---------------------------------------------------
    def check_exits(self, quotes: dict, now: float, alerts: dict | None = None) -> None:
        for sym, pos in list(self.book.positions.items()):
            if sym in quotes:
                why = self.risk.exit_reason(pos, quotes[sym], now)
                if why:
                    self.close(sym, quotes[sym], now, why, alerts)

    def mark(self, quotes: dict, t: float, last_resort_quotes: dict, alerts: dict | None = None) -> dict:
        m = self.book.mark(quotes, t)
        self.last_mark = m
        kill = self.risk.kill_check(m)
        if kill and not self.book.halted:
            kind, reason = kill
            n = 0
            for sym in list(self.book.positions):
                if sym in last_resort_quotes:
                    self.close(sym, last_resort_quotes[sym], t, f"kill switch: {reason}")
                    n += 1
            self.book.halt(kind, reason, t)
            text = (f"KILL SWITCH ({kind}): {reason}. Closed {n} position(s). "
                    + ("No new trades until 00:00 UTC." if kind == "daily" else "Trading stays off until reset by hand."))
            self.event(text)
            if (alerts or {}).get("kill_switch", True):
                self.alert(f"🛑 {'' if self.prefix else 'Jev Desk '}{text}" + ("" if kind == "daily" else " Reset: bash ~/paper-desk/server/reset-kill.sh"))
        return m
