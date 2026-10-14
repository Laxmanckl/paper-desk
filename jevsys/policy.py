"""Stage 3: the policy engine (guide Step 5, `compose_action`, adapted to directional trading).

Code, not the model, turns Jev's answers into an action. Every threshold lives
in config/desk.json, and entering needs a higher bar than exiting, because a
wrong entry costs more than leaving early.

Order of checks, for an instrument WITH a position:
  1. exit urgency "Close now" (confident)          -> EXIT
  2. Jev confidently points the other way          -> EXIT
  3. exit urgency "Tighten the stop" (confident)   -> TIGHTEN (stop to halfway, or breakeven once in profit)
  4. otherwise                                     -> HOLD
For an instrument WITHOUT a position:
  1. costs abnormal                                -> STAND ASIDE
  2. regime volatile or quiet (confident)          -> STAND ASIDE
  3. direction neutral, or not confident enough    -> STAND ASIDE
  4. entry quality below Good, or not confident    -> STAND ASIDE
  5. move unlikely to continue                     -> STAND ASIDE
  6. all clear                                     -> ENTER LONG / ENTER SHORT (then the risk engine decides)
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Action:
    kind: str                 # ENTER | EXIT | TIGHTEN | HOLD | STAND_ASIDE
    side: str | None = None   # long | short for ENTER
    why: str = ""
    confident: bool = False   # Jev's reads cleared the confidence gates (for the ladder)
    checks: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "side": self.side, "why": self.why, "confident": self.confident, "checks": self.checks}


def decide(ans: dict, position_side: str | None, p: dict) -> Action:
    rg, dr = ans.get("regime", {}), ans.get("direction", {})
    mc, ca = ans.get("move_continues", {}), ans.get("costs_abnormal", {})
    eq, xu = ans.get("entry_quality", {}), ans.get("exit_urgency", {})
    dir_conf = dr.get("p", 0.0)

    if position_side:
        if xu.get("level", 0) >= p["exit_urgency_close_level"] and xu.get("p", 0) >= p["exit_urgency_conf"]:
            return Action("EXIT", why=f"exit urgency '{xu.get('label')}' at {xu['p']:.0%}", confident=True)
        opposite = "down" if position_side == "long" else "up"
        if dr.get("value") == opposite and dir_conf >= p["exit_on_opposite_direction_conf"]:
            return Action("EXIT", why=f"Jev now points {opposite} at {dir_conf:.0%}", confident=True)
        if xu.get("level", 0) >= p["exit_urgency_tighten_level"] and xu.get("p", 0) >= p["exit_urgency_conf"]:
            return Action("TIGHTEN", why=f"exit urgency '{xu.get('label')}' at {xu['p']:.0%}", confident=True)
        return Action("HOLD", why="no exit signal; stop and target in place", confident=dir_conf >= p["enter_direction_conf"])

    checks = []

    def gate(ok: bool, text: str) -> bool:
        checks.append({"ok": ok, "check": text})
        return ok

    if not gate(ca.get("p", 1.0) < p["stand_aside_costs_abnormal"],
                f"costs normal (abnormal {ca.get('p', 0):.0%} < {p['stand_aside_costs_abnormal']:.0%})"):
        return Action("STAND_ASIDE", why=f"costs abnormal ({ca.get('p', 0):.0%})", checks=checks)
    bad_regime = rg.get("value") in p["stand_aside_regimes"] and rg.get("p", 0) >= p["stand_aside_regime_conf"]
    if not gate(not bad_regime, f"regime tradable ({rg.get('value')} {rg.get('p', 0):.0%})"):
        return Action("STAND_ASIDE", why=f"regime {rg.get('value')} ({rg.get('p', 0):.0%})", checks=checks)
    confident = dr.get("value") in ("up", "down") and dir_conf >= p["enter_direction_conf"]
    if not gate(confident, f"direction clear ({dr.get('value')} {dir_conf:.0%} vs {p['enter_direction_conf']:.0%})"):
        why = "no clear direction" if dr.get("value") == "neutral" else f"direction {dr.get('value')} only {dir_conf:.0%}"
        return Action("STAND_ASIDE", why=why, checks=checks)
    good_entry = eq.get("level", 0) >= p["enter_quality_min_level"] and eq.get("p", 0) >= p["enter_quality_conf"]
    if not gate(good_entry, f"entry quality ({eq.get('label')} {eq.get('p', 0):.0%})"):
        return Action("STAND_ASIDE", why=f"entry quality '{eq.get('label')}' ({eq.get('p', 0):.0%})", confident=True, checks=checks)
    if not gate(mc.get("p", 0) >= p["enter_move_continues_min"],
                f"move continues ({mc.get('p', 0):.0%} vs {p['enter_move_continues_min']:.0%})"):
        return Action("STAND_ASIDE", why=f"move may reverse (continues {mc.get('p', 0):.0%})", confident=True, checks=checks)
    side = "long" if dr["value"] == "up" else "short"
    return Action("ENTER", side=side, why=f"{dr['value']} {dir_conf:.0%}, entry '{eq.get('label')}' {eq.get('p', 0):.0%}, "
                  f"{rg.get('value')} regime", confident=True, checks=checks)


def tightened_stop(side: str, entry: float, stop: float, price: float, digits: int) -> float:
    """Halfway from the stop to the current price; never past breakeven once the trade is in profit,
    and never looser than the current stop (the paper book enforces that too)."""
    half = stop + (price - stop) / 2
    in_profit = (price > entry) if side == "long" else (price < entry)
    new = max(half, entry) if side == "long" and in_profit else min(half, entry) if side == "short" and in_profit else half
    return round(new, digits)
