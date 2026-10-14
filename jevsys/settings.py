"""Loads config/desk.json (policy thresholds + risk limits).

The file is re-read whenever it changes on disk (the server's GitHub pull
brings edits in), so thresholds can be tuned without a restart. An invalid
edit is rejected and the previous settings stay in force. Every decision logs
the settings version it used.
"""
from __future__ import annotations

import hashlib
import json
import os

DEFAULTS = {
    "policy": {
        "enter_direction_conf": 0.65, "enter_quality_min_level": 2, "enter_quality_conf": 0.45,
        "enter_move_continues_min": 0.55, "use_jev_costs": False, "stand_aside_costs_abnormal": 0.6,
        "stand_aside_regimes": ["volatile", "quiet"], "stand_aside_regime_conf": 0.6,
        "exit_urgency_close_level": 3, "exit_urgency_tighten_level": 2, "exit_urgency_conf": 0.5,
        "exit_on_opposite_direction_conf": 0.6,
    },
    "risk": {
        "risk_per_trade_pct": 0.5, "stop_atr_mult": 2.5, "target_r_multiple": 1.5, "max_lots": 1.0,
        "max_open_positions": 4, "max_same_usd_direction": 2, "max_daily_loss_pct": 2.0,
        "max_drawdown_pct": 6.0, "max_spread_vs_normal": 2.0, "max_quote_age_s": 60,
        "max_jev_errors_in_a_row": 3, "max_decision_ms": 5000, "max_hold_hours": 6,
        "friday_no_entries_utc": "19:00", "friday_flat_utc": "20:30", "commission_per_lot_usd": 0.0,
        "max_leverage": 10.0,
    },
    "fallback": {
        "enabled": True, "after_failed_rounds": 2, "risk_mult": 0.5,
        "min_ema20_dist_atr": 0.5, "min_ema20_slope_atr": 0.08,
        "rsi_long_min": 50, "rsi_long_max": 70, "rsi_short_min": 30, "rsi_short_max": 50,
        "exit_rsi_long": 78, "exit_rsi_short": 22, "max_spread_vs_normal": 1.5,
    },
    "alerts": {
        "trades": True, "kill_switch": True, "jev_down": True, "bridge_down_min": 5,
        "daily_summary_utc": "21:05",
    },
}

# (min, max) sanity bounds; a value outside them rejects the whole edit
BOUNDS = {
    "enter_direction_conf": (0.34, 1), "enter_quality_min_level": (0, 3), "enter_quality_conf": (0, 1),
    "enter_move_continues_min": (0, 1), "stand_aside_costs_abnormal": (0, 1), "stand_aside_regime_conf": (0, 1),
    "exit_urgency_close_level": (1, 3), "exit_urgency_tighten_level": (1, 3), "exit_urgency_conf": (0, 1),
    "exit_on_opposite_direction_conf": (0.34, 1),
    "risk_per_trade_pct": (0.01, 2), "stop_atr_mult": (0.5, 20), "target_r_multiple": (0.3, 10), "max_lots": (0.01, 10),
    "max_open_positions": (0, 8), "max_same_usd_direction": (0, 8), "max_daily_loss_pct": (0.1, 20),
    "max_drawdown_pct": (0.5, 50), "max_spread_vs_normal": (1, 10), "max_quote_age_s": (5, 600),
    "max_jev_errors_in_a_row": (1, 100), "max_decision_ms": (100, 20_000), "max_hold_hours": (0.25, 120),
    "commission_per_lot_usd": (0, 50), "max_leverage": (0.5, 100),
    "after_failed_rounds": (1, 50), "risk_mult": (0.05, 1), "min_ema20_dist_atr": (0, 10), "min_ema20_slope_atr": (0, 5),
    "rsi_long_min": (0, 100), "rsi_long_max": (0, 100), "rsi_short_min": (0, 100), "rsi_short_max": (0, 100),
    "exit_rsi_long": (50, 100), "exit_rsi_short": (0, 50), "max_spread_vs_normal": (1, 10), "bridge_down_min": (1, 600),
}


def _hhmm(v: str) -> int:
    h, m = v.split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(v)
    return h * 60 + m


def validate(raw: dict) -> dict:
    out = {}
    for section, defaults in DEFAULTS.items():
        given = {k: v for k, v in (raw.get(section) or {}).items() if not k.startswith("_")}
        unknown = set(given) - set(defaults)
        if unknown:
            raise ValueError(f"unknown setting(s) in {section}: {', '.join(sorted(unknown))}")
        merged = {**defaults, **given}
        for k, v in merged.items():
            if k in BOUNDS:
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise ValueError(f"{section}.{k} must be a number")
                lo, hi = BOUNDS[k]
                if not lo <= v <= hi:
                    raise ValueError(f"{section}.{k}={v} is outside {lo}..{hi}")
        if section == "policy":
            if not isinstance(merged["stand_aside_regimes"], list):
                raise ValueError("policy.stand_aside_regimes must be a list")
            if merged["exit_urgency_tighten_level"] > merged["exit_urgency_close_level"]:
                raise ValueError("exit_urgency_tighten_level must not exceed exit_urgency_close_level")
        if section == "risk":
            _hhmm(merged["friday_no_entries_utc"])
            _hhmm(merged["friday_flat_utc"])
        if section == "alerts":
            _hhmm(merged["daily_summary_utc"])
        for k in ("enabled", "trades", "kill_switch", "jev_down", "use_jev_costs"):
            if k in merged and not isinstance(merged[k], bool):
                raise ValueError(f"{section}.{k} must be true or false")
        out[section] = merged
    return out


class Settings:
    def __init__(self, path: str = "config/desk.json"):
        self.path = path
        self._mtime = None
        self.data = validate({})
        self.version = "defaults"
        self.error: str | None = None
        self.reload()

    def reload(self) -> str | None:
        """Re-read if the file changed. Returns a message when something happened."""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return None
        if mtime == self._mtime:
            return None
        self._mtime = mtime
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = fh.read()
            data = validate(json.loads(raw))
        except (ValueError, json.JSONDecodeError) as e:
            self.error = str(e)
            return f"Settings edit rejected, keeping version {self.version}: {e}"
        old = self.version
        self.data, self.error = data, None
        self.version = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:8]
        return None if old == self.version else (f"Settings loaded (version {self.version})."
                                                 if old == "defaults" else f"Settings changed: {old} -> {self.version}.")

    @property
    def policy(self) -> dict:
        return self.data["policy"]

    @property
    def risk(self) -> dict:
        return self.data["risk"]

    @property
    def fallback(self) -> dict:
        return self.data["fallback"]

    @property
    def alerts(self) -> dict:
        return self.data["alerts"]

    def minutes(self, key: str, section: str = "risk") -> int:
        return _hhmm(self.data[section][key])
