"""Entry plans: the contract between an intraday strategy and the engine.

The legacy strategies only say *when* to trade (a +1/-1 signal) and the engine
derives the stop and target from ATR.  The intraday strategies need stops and
targets that come from market structure (the opposite side of a range, the
wick of a fakeout bar, the daily strategy's own target), so they publish an
``EntryPlan`` per bar instead.

A plan is computed on the **close of bar t** and executed at the **open of bar
t+1**, exactly like a signal.  Distances are from the *fill* price, which is
not known when the plan is built, so the plan stores either

* ``sl_dist`` / ``tp_dist`` - price distances from the fill, or
* ``tp_price`` - an absolute chart price (used when the target is a level such
  as the range midpoint rather than a multiple of risk).

Nothing here reads a future bar: every field is derivable from bars ``<= t``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

__all__ = ["EntryPlan", "empty_plans", "plan_directions", "plans_to_frame"]


@dataclass(frozen=True)
class EntryPlan:
    """A stop/target pair for the next bar's open."""

    direction: int                 # +1 long / -1 short
    sl_dist: float                 # stop distance from the fill, in price
    tp_dist: float | None = None   # target distance from the fill, in price
    tp_price: float | None = None  # absolute target level (wins over tp_dist)
    note: str = ""

    def is_valid(self) -> bool:
        if self.direction not in (-1, 1):
            return False
        if not np.isfinite(self.sl_dist) or self.sl_dist <= 0:
            return False
        if self.tp_price is not None and not np.isfinite(self.tp_price):
            return False
        if self.tp_dist is not None and (not np.isfinite(self.tp_dist) or self.tp_dist <= 0):
            return False
        return True


def empty_plans(index: pd.Index | int) -> np.ndarray:
    """Object array of ``None`` sized to ``index``."""
    n = index if isinstance(index, int) else len(index)
    out = np.empty(n, dtype=object)
    out[:] = None
    return out


def plan_directions(plans: np.ndarray) -> np.ndarray:
    """+1 / -1 / 0 per bar, taken from the plans (live runner compatibility)."""
    out = np.zeros(len(plans), dtype=int)
    for i, p in enumerate(plans):
        if p is not None:
            out[i] = p.direction
    return out


def plans_to_frame(index: pd.Index, plans: np.ndarray) -> pd.DataFrame:
    """Debug/inspection helper: one row per plan."""
    rows = []
    for ts, p in zip(index, plans):
        if p is None:
            continue
        rows.append({"time": ts, "direction": p.direction, "sl_dist": p.sl_dist,
                     "tp_dist": p.tp_dist, "tp_price": p.tp_price, "note": p.note})
    return pd.DataFrame(rows)
