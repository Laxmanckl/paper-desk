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
        "enter_quality_p_good_min": 0.30, "use_move_continues": False,
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
    # London breakout: Asian range (00:00-07:00 UTC) → entry 07:00-11:00 UTC.
    # The only session validated on real MT5 data (16 years, TEST +0.13R, PF 1.22).
    "vbo": {
        "enabled": True, "tp_r": 2.0, "min_rvol": 1.5, "sl_cap_atr": 2.0,
        "risk_per_trade_pct": 0.5, "max_open_positions": 3, "max_daily_loss_pct": 2.0,
        "max_consecutive_losses": 4, "max_drawdown_pct": 10.0, "max_lots": 2.0, "max_leverage": 30.0,
        "range_start_utc": 0, "range_end_utc": 7, "entry_start_utc": 7, "entry_end_utc": 11,
        "exit_utc_hour": 20, "news_window_min": 30, "max_quote_age_s": 60,
    },
    # NY breakout: London range (07:00-13:00 UTC) → entry 13:00-18:00 UTC.
    # Same recipe, different window. Not individually validated on real MT5 data;
    # judge after a few months and turn off if it's losing.
    "vbo_ny": {
        "enabled": True, "tp_r": 2.0, "min_rvol": 1.5, "sl_cap_atr": 2.0,
        "risk_per_trade_pct": 0.5, "max_open_positions": 3, "max_daily_loss_pct": 2.0,
        "max_consecutive_losses": 4, "max_drawdown_pct": 10.0, "max_lots": 2.0, "max_leverage": 30.0,
        "range_start_utc": 7, "range_end_utc": 13, "entry_start_utc": 13, "entry_end_utc": 18,
        "exit_utc_hour": 21, "news_window_min": 30, "max_quote_age_s": 60,
    },
    # Overnight breakout (fed into the Asia session): NY range (13:00-20:00 UTC)
    # → entry 20:00-23:00 UTC = 01:30-04:30 Dubai. Same recipe, different window.
    # Not validated on real data; experimental.
    "vbo_asia": {
        "enabled": True, "tp_r": 2.0, "min_rvol": 1.5, "sl_cap_atr": 2.0,
        "risk_per_trade_pct": 0.5, "max_open_positions": 3, "max_daily_loss_pct": 2.0,
        "max_consecutive_losses": 4, "max_drawdown_pct": 10.0, "max_lots": 2.0, "max_leverage": 30.0,
        "range_start_utc": 13, "range_end_utc": 20, "entry_start_utc": 20, "entry_end_utc": 23,
        "exit_utc_hour": 23, "news_window_min": 30, "max_quote_age_s": 60,
    },
    "alerts": {
        "trades": True, "kill_switch": True, "jev_down": True, "bridge_down_min": 5,
        "daily_summary_utc": "21:05",
    },
}

# (min, max) sanity bounds; a value outside them rejects the whole edit
BOUNDS = {
    "enter_direction_conf": (0.34, 1), "enter_quality_min_level": (0, 3), "enter_quality_conf": (0, 1), "enter_quality_p_good_min": (0, 1),
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
    "tp_r": (0.5, 5), "min_rvol": (1, 5), "sl_cap_atr": (0.5, 10), "max_consecutive_losses": (1, 20),
    "exit_utc_hour": (3, 23), "news_window_min": (0, 240),
    "range_start_utc": (0, 23), "range_end_utc": (1, 24),
    "entry_start_utc": (0, 23), "entry_end_utc": (1, 24),
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
        for k in ("enabled", "trades", "kill_switch", "jev_down", "use_jev_costs", "use_move_continues"):
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
    def vbo(self) -> dict:
        return self.data["vbo"]

    @property
    def vbo_ny(self) -> dict:
        return self.data["vbo_ny"]

    @property
    def vbo_asia(self) -> dict:
        return self.data["vbo_asia"]

    def vbo_session(self, name: str) -> dict:
        """Settings for a VBO session: 'london' (the 'vbo' section), 'ny' or 'asia'."""
        key = "vbo" if name == "london" else f"vbo_{name}"
        return self.data[key]

    @property
    def alerts(self) -> dict:
        return self.data["alerts"]

    def minutes(self, key: str, section: str = "risk") -> int:
        return _hhmm(self.data[section][key])
