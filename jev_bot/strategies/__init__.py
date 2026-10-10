"""Daily strategies in one common shape (see base.py and _template.py).

Each module defines STRATEGY, an instance of DailyStrategy. Modules whose names
start with "_" (the template) are never run by the live desk.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path

from .base import DailyStrategy

HERE = Path(__file__).resolve().parent


def available() -> list[str]:
    return sorted(m.name for m in pkgutil.iter_modules([str(HERE)])
                  if not m.name.startswith("_") and m.name != "base")


def load(name: str) -> DailyStrategy:
    if not re.fullmatch(r"_?[a-z][a-z0-9_]*", name or ""):
        raise SystemExit(f"  bad strategy name {name!r}")
    try:
        mod = importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError:
        raise SystemExit(f"  no strategy jev_bot/strategies/{name}.py "
                         f"(have: {', '.join(available()) or 'none yet'})") from None
    strat = getattr(mod, "STRATEGY", None)
    if not isinstance(strat, DailyStrategy):
        raise SystemExit(f"  jev_bot/strategies/{name}.py must define STRATEGY = <a DailyStrategy>")
    return strat


def for_hypothesis(h: dict) -> DailyStrategy | None:
    """The strategy a ledger entry is wired to, if it is one of these modules."""
    name = h.get("module")
    return load(name) if name else None


def check(strat: DailyStrategy, sim_bars: int = 400) -> list[str]:
    """Step 3's unit tests. Returns the failures (empty = passed).

    - its own tests(): hand-made histories with the expected signal
    - only "BUY", "SELL" or None ever comes back
    - the same history always gives the same signal (no hidden randomness/state)
    - exits and sizing are sane numbers
    """
    from .. import fx
    fails = []
    cases = strat.tests()
    if not cases:
        fails.append("tests() is empty: add at least one BUY, one SELL and one no-signal case")
    for desc, hist, want in cases:
        try:
            got = strat.signal(list(hist))
        except Exception as e:
            fails.append(f"test '{desc}' crashed: {type(e).__name__}: {e}")
            continue
        if got != want:
            fails.append(f"test '{desc}': expected {want}, got {got}")
    for sym in strat.markets:
        bars = fx.simulate(fx.resolve(sym), sim_bars, 11)
        seen = set()
        for t in range(1, len(bars)):
            hist = bars[:t + 1]
            try:
                a, b = strat.signal(hist), strat.signal(list(hist))
                strat.conditions(hist)
                strat.explain(hist)
            except Exception as e:
                fails.append(f"{sym} bar {t}: crashed ({type(e).__name__}: {e})")
                break
            if a not in ("BUY", "SELL", None):
                fails.append(f"{sym} bar {t}: returned {a!r}; only 'BUY', 'SELL' or None")
                break
            if a != b:
                fails.append(f"{sym} bar {t}: same history gave {a} then {b} (hidden state or randomness)")
                break
            seen.add(a)
        if not ({"BUY", "SELL"} & seen):
            fails.append(f"{sym}: never signalled in {sim_bars} simulated days")
    if not (0 < strat.risk_pct <= 0.03):
        fails.append(f"risk_pct {strat.risk_pct} outside 0-3% per trade")
    if not (strat.sl_atr > 0 and strat.tp_atr > 0 and strat.max_hold > 0):
        fails.append("sl_atr, tp_atr and max_hold must all be positive")
    if not strat.hypothesis:
        fails.append("hypothesis is empty: one line saying what idea this tests")
    return fails
