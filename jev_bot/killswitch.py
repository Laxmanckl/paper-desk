"""The kill switch. Hard limits in code, read from config/risk.json.

Every NEW trade on every desk passes through `check_order` first. If it says
no, the trade never happens, whatever the signal or the decision engine says.
A limit written in a prompt is a suggestion; this is not.

    config/risk.json
      kill_all                  true = no new trades anywhere
      desks.<desk>.enabled      false = this desk opens nothing new (a "pause")
      max_daily_loss_pct        down this much since 00:00 UTC -> stop for the day
      max_drawdown_pct          down this much from the account's peak -> stop
                                until a human looks (raise the limit, or set
                                peak_reset to any new text to measure from now)
      max_open_positions        open trades across the desk
      max_order_leverage        one order's size / equity

Open trades are never touched: they keep their own stops and targets.

Fails closed: if the file exists but cannot be read, nothing new trades.
If the file is missing, the defaults below apply (never "no limits").
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "config" / "risk.json"

DESKS = {"daily": "Daily bot", "scalper_a": "Scalper A · Pullback", "scalper_orb": "Scalper B · Breakout"}

DEFAULTS = {
    "enabled": True,
    "max_daily_loss_pct": 2.0,
    "max_drawdown_pct": 8.0,
    "max_open_positions": 5,
    "max_order_leverage": 10.0,
    "peak_reset": "",
}

MAX_BLOCKS = 20
_cache: dict = {}


def load(path: str | os.PathLike | None = None) -> dict:
    """The whole config, re-read only when the file changes (cheap to call per trade)."""
    p = Path(path or PATH)
    try:
        mtime = p.stat().st_mtime
    except FileNotFoundError:
        return {"kill_all": False, "desks": {}, "_missing": True}
    key = str(p)
    hit = _cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("top level must be an object")
    except (OSError, ValueError) as e:
        data = {"_error": f"config/risk.json unreadable ({e})"}
    _cache[key] = (mtime, data)
    return data


def limits(desk: str, cfg: dict | None = None) -> dict:
    """A promoted strategy's desk ("daily:H4") uses its own section if there is one,
    else its parent desk's ("daily")."""
    cfg = load() if cfg is None else cfg
    desks = cfg.get("desks") or {}
    parent = desks.get(desk.split(":")[0]) or {} if ":" in desk else {}
    return {**DEFAULTS, **parent, **(desks.get(desk) or {})}


def _numbers(acct: dict) -> dict:
    eq = float(acct.get("equity") or 0.0)
    peak = max(float(acct.get("peak") or 0.0), eq) or eq
    day = float(acct.get("day_start") or 0.0) or eq
    return {"equity": eq, "peak": peak, "day_start": day,
            "daily_loss_pct": max(0.0, (day - eq) / day * 100) if day else 0.0,
            "drawdown_pct": max(0.0, (peak - eq) / peak * 100) if peak else 0.0,
            "open": int(acct.get("open_positions") or 0)}


def check_order(desk: str, order: dict, acct: dict, cfg: dict | None = None) -> tuple[bool, str]:
    """May this order go ahead?  order: {"notional": USD size}.
    acct: {"equity", "peak", "day_start", "open_positions"}.  Returns (ok, reason)."""
    cfg = load() if cfg is None else cfg
    if cfg.get("_error"):
        return False, cfg["_error"] + "; nothing new trades until it is fixed"
    if cfg.get("kill_all"):
        return False, "kill switch: kill_all is on in config/risk.json"
    lim, n = limits(desk, cfg), _numbers(acct)
    if not lim["enabled"]:
        return False, f"kill switch: {name(desk)} is paused (enabled: false)"
    if n["daily_loss_pct"] >= lim["max_daily_loss_pct"]:
        return False, (f"kill switch: down {n['daily_loss_pct']:.2f}% today, limit "
                       f"{lim['max_daily_loss_pct']:g}%; trading stopped until 00:00 UTC")
    if n["drawdown_pct"] >= lim["max_drawdown_pct"]:
        return False, (f"kill switch: {n['drawdown_pct']:.2f}% below the peak, limit "
                       f"{lim['max_drawdown_pct']:g}%; trading stopped until a human reviews it")
    if n["open"] >= lim["max_open_positions"]:
        return False, f"kill switch: {n['open']} trades open, limit {lim['max_open_positions']}"
    notional = float(order.get("notional") or 0.0)
    if n["equity"] > 0 and notional / n["equity"] > lim["max_order_leverage"]:
        return False, (f"kill switch: order is {notional / n['equity']:.1f}x equity, "
                       f"limit {lim['max_order_leverage']:g}x")
    return True, "OK"


def track(desk: str, holder, equity: float, cfg: dict | None = None) -> None:
    """Keep holder.peak_equity up to date (holder: a daily Account or a ScalpBook).
    Changing peak_reset in risk.json restarts the peak from the current equity."""
    cfg = load() if cfg is None else cfg
    token = str(limits(desk, cfg).get("peak_reset") or "")
    ks = holder.killswitch
    if token != ks.get("peak_reset", ""):
        ks["peak_reset"] = token
        holder.peak_equity = equity
        return
    if not holder.peak_equity:                       # first time: start from the account's history
        hist = getattr(holder, "history_peak", None)
        holder.peak_equity = hist() if hist else holder.start_equity
    holder.peak_equity = max(holder.peak_equity, equity)


def note_block(holder, reason: str, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    blocks = holder.killswitch.setdefault("blocks", [])
    if blocks and blocks[-1][1] == reason:
        blocks[-1][0] = now.isoformat(timespec="seconds")     # same reason again: just refresh the time
    else:
        blocks.append([now.isoformat(timespec="seconds"), reason])
    del blocks[:-MAX_BLOCKS]


def name(desk: str) -> str:
    if ":" in desk:
        return f"{DESKS.get(desk.split(':')[0], desk)} · {desk.split(':', 1)[1]}"
    return DESKS.get(desk, desk)


def status(desk: str, acct: dict, blocks: list | None = None, cfg: dict | None = None) -> dict:
    """Everything the dashboards show about the kill switch for one desk."""
    cfg = load() if cfg is None else cfg
    lim, n = limits(desk, cfg), _numbers(acct)
    ok, reason = check_order(desk, {"notional": 0.0}, {**acct, "open_positions": 0}, cfg)
    meters = [
        {"key": "daily_loss", "label": "Loss today", "value": round(n["daily_loss_pct"], 2),
         "limit": lim["max_daily_loss_pct"], "unit": "%"},
        {"key": "drawdown", "label": "Below peak", "value": round(n["drawdown_pct"], 2),
         "limit": lim["max_drawdown_pct"], "unit": "%"},
        {"key": "open", "label": "Open trades", "value": n["open"],
         "limit": lim["max_open_positions"], "unit": ""},
    ]
    for m in meters:
        m["frac"] = round(min(1.0, m["value"] / m["limit"]), 3) if m["limit"] else 0.0
    state = "halted" if not ok else ("warning" if any(m["frac"] >= 0.75 and m["key"] != "open" for m in meters) else "ok")
    return {"desk": desk, "name": name(desk), "state": state, "reason": "" if ok else reason,
            "meters": meters, "max_order_leverage": lim["max_order_leverage"], "enabled": lim["enabled"],
            "kill_all": bool(cfg.get("kill_all")), "peak": round(n["peak"], 2),
            "config_missing": bool(cfg.get("_missing")), "blocks": (blocks or [])[-5:]}
