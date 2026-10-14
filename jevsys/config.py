"""Editable settings. Later stages add policy thresholds and risk limits here."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StateConfig:
    max_snapshot_tokens: int = 400       # guide Step 3 budget
    vol_short_s: float = 60.0            # short realized-vol window
    vol_medium_s: float = 900.0          # medium realized-vol window
    vol_baseline_s: float = 86_400.0     # 24h comparison window
    flow_window_s: float = 10.0          # aggressive volume / intensity window
    return_horizons_s: tuple[int, ...] = (60, 300, 1800)  # 1, 5, 30 min
    depth_levels: int = 3
    latency_samples: int = 10
    health_window: int = 200             # recent orders for fill/reject ratios
    max_ref_age_s: float = 5.0           # stale reference price is dropped
