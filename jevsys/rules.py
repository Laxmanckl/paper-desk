"""Stage 5: simple rules written in code only (guide Step 6, fallback rung 4).

Used when Jev is unavailable, and run every round in a shadow paper account
as the "plain hand-written rules" baseline Step 8 compares the full system
against. Plain trend-following with strict filters. It reads only the
snapshot's own numbers.

Flat:
  long  if price >= min_ema20_dist_atr ATRs above the 20-bar EMA, the EMA slopes up
        by >= min_ema20_slope_atr, price is above the 100-bar EMA, and RSI is in
        [rsi_long_min, rsi_long_max]
  short mirror image
  stand aside otherwise, or when the spread is wider than max_spread_vs_normal
In a position:
  exit  when price crosses back over the 20-bar EMA, or RSI reaches exit_rsi_long / exit_rsi_short
  hold  otherwise (the stop and target set by the risk engine stay in force)
"""
from __future__ import annotations

from .policy import Action

# "Jev without the confidence gate": act on Jev's top answer whatever its
# confidence. Step 8 compares this with the gated policy, to see whether
# standing aside when Jev is unsure actually helps.
NO_GATE = {
    "enter_direction_conf": 0.0, "enter_quality_min_level": 2, "enter_quality_conf": 0.0,
    "enter_move_continues_min": 0.5, "use_jev_costs": False, "stand_aside_costs_abnormal": 0.5,
    "stand_aside_regimes": ["volatile", "quiet"], "stand_aside_regime_conf": 0.0,
    "exit_urgency_close_level": 3, "exit_urgency_tighten_level": 2, "exit_urgency_conf": 0.0,
    "exit_on_opposite_direction_conf": 0.0,
}


def decide_rules(snap: dict, position_side: str | None, f: dict) -> Action:
    dist, slope = snap.get("ema20_dist_atr"), snap.get("ema20_slope_atr")
    dist100, rsi = snap.get("ema100_dist_atr"), snap.get("rsi14")
    if None in (dist, slope, dist100, rsi):
        return Action("HOLD" if position_side else "STAND_ASIDE", why="rules: not enough history for EMAs/RSI")

    if position_side == "long":
        if dist < 0:
            return Action("EXIT", why=f"rules: price back below EMA 20 ({dist:+.2f} ATR)")
        if rsi >= f["exit_rsi_long"]:
            return Action("EXIT", why=f"rules: RSI {rsi:.0f} stretched")
        return Action("HOLD", why="rules: trend intact")
    if position_side == "short":
        if dist > 0:
            return Action("EXIT", why=f"rules: price back above EMA 20 ({dist:+.2f} ATR)")
        if rsi <= f["exit_rsi_short"]:
            return Action("EXIT", why=f"rules: RSI {rsi:.0f} stretched")
        return Action("HOLD", why="rules: trend intact")

    svn = snap.get("spread_vs_norm")
    if svn is not None and svn > f["max_spread_vs_normal"]:
        return Action("STAND_ASIDE", why=f"rules: spread {svn:.1f}x normal")
    if (dist >= f["min_ema20_dist_atr"] and slope >= f["min_ema20_slope_atr"] and dist100 > 0
            and f["rsi_long_min"] <= rsi <= f["rsi_long_max"]):
        return Action("ENTER", side="long", why=f"rules: uptrend ({dist:+.2f} ATR over EMA 20, RSI {rsi:.0f})")
    if (dist <= -f["min_ema20_dist_atr"] and slope <= -f["min_ema20_slope_atr"] and dist100 < 0
            and f["rsi_short_min"] <= rsi <= f["rsi_short_max"]):
        return Action("ENTER", side="short", why=f"rules: downtrend ({dist:+.2f} ATR under EMA 20, RSI {rsi:.0f})")
    return Action("STAND_ASIDE", why="rules: no clean trend")
