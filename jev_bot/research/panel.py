"""What the dashboards show about the research loop, for one desk.

    kill switch     state, the three meters, recent blocks
    hypothesis      the desk's ledger entry: prediction | backtest | sealed | paper
    review          the latest weekly critic findings for the desk
    ledger          every idea ever tested (the daily page shows the full table)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .. import killswitch
from . import ledger, stats

HTML = Path(__file__).with_name("panel.html")
REVIEW_DIR = Path(__file__).resolve().parent.parent.parent / "state" / "reviews"
_cache: dict = {}


def _cached_json(path: Path, default):
    try:
        m = path.stat().st_mtime
    except FileNotFoundError:
        return default
    hit = _cache.get(str(path))
    if hit and hit[0] == m:
        return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = default
    _cache[str(path)] = (m, data)
    return data


def latest_review() -> dict:
    return _cached_json(REVIEW_DIR / "latest.json", {})


def ledger_data() -> dict:
    return _cached_json(ledger.PATH, {"hypotheses": []})


# --- paper results, the same way for every desk --------------------------------------

def paper_daily(acct) -> dict:
    closed = acct.closed or []
    eq = acct.equity()
    curve = [acct.start_equity] + [p[1] for p in (acct.equity_log or [])] + [eq]
    wins = sum(1 for t in closed if t["pnl"] > 0)
    rs = [t["pnl"] / r for t in closed if (r := abs(t["entry"] - t["stop"]) * t["units"])]
    return {"return_pct": round((eq / acct.start_equity - 1) * 100, 2), "trades": len(closed),
            "win_rate_pct": round(100 * wins / len(closed), 1) if closed else 0.0,
            "max_drawdown_pct": round(stats.max_drawdown(curve) * 100, 2),
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None, "since": (acct.created or "")[:10],
            "open": len(acct.positions)}


def paper_scalp(book) -> dict:
    s = (book.stats or {}).get("_all", {})
    eq = book.equity()
    run, peak, mdd = book.start_equity, book.start_equity, 0.0
    for t in book.trades:
        run += t["pnl"]
        peak = max(peak, run)
        mdd = max(mdd, (peak - run) / peak)
    if book.peak_equity:
        mdd = max(mdd, (book.peak_equity - eq) / book.peak_equity)
    rs = [t["r"] for t in book.trades if t.get("r") is not None]
    return {"return_pct": round((eq / book.start_equity - 1) * 100, 2), "trades": s.get("n", 0),
            "win_rate_pct": round(100 * s.get("wins", 0) / s["n"], 1) if s.get("n") else 0.0,
            "max_drawdown_pct": round(max(0.0, mdd) * 100, 2),
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None, "since": (book.created or "")[:10],
            "open": len(book.positions)}


# --- compact shapes for the pages ----------------------------------------------------

def _compact(h: dict) -> dict:
    bt = h.get("backtest") or None
    if bt:
        bt = {k: bt.get(k) for k in ("at", "version", "period", "sealed_from", "data", "return_pct", "win_rate_pct",
                                     "max_drawdown_pct", "trades", "sharpe_ann", "dsr", "checks", "passed",
                                     "walk_forward", "costs_2x", "breaker", "worst_stretches", "regime")}
    return {"id": h["id"], "idea": h.get("idea", ""), "desk": h.get("desk"), "markets": h.get("markets", []),
            "source": h.get("source", ""), "status": h.get("status"),
            "status_label": ledger.STATUSES.get(h.get("status"), h.get("status")),
            "version": h.get("version", 1), "variations": h.get("variations", 0),
            "max_variations": h.get("max_variations", 5), "falsify": h.get("falsify", ""),
            "prediction": h.get("prediction"), "prediction_intact": ledger.prediction_intact(h),
            "regime": h.get("regime", ""), "backtest": bt, "sealed": h.get("sealed"),
            "paper": h.get("paper"), "lessons": (h.get("lessons") or [])[-4:], "notes": h.get("notes", ""),
            "created": (h.get("created") or "")[:10]}


def primary(data: dict, desk: str) -> dict | None:
    hs = ledger.for_desk(data, desk)
    live = [h for h in hs if h.get("status") in ("paper", "passed_paper", "paused")]
    return (live or hs or [None])[-1]


def ledger_rows(data: dict) -> list[dict]:
    return [{"id": h["id"], "idea": h.get("idea", ""), "desk": h.get("desk"), "markets": h.get("markets", []),
             "source": h.get("source", ""), "regime": h.get("regime", ""), "status": h.get("status"),
             "status_label": ledger.STATUSES.get(h.get("status"), h.get("status")),
             "variations": h.get("variations", 0), "max_variations": h.get("max_variations", 5),
             "lesson": ((h.get("lessons") or [{}])[-1]).get("text", ""), "created": (h.get("created") or "")[:10]}
            for h in reversed(data.get("hypotheses", []))]


def for_desk(desk: str, risk_numbers: dict, blocks: list, paper: dict, full_ledger: bool = False) -> dict:
    data = ledger_data()
    h = primary(data, desk)
    rv = latest_review()
    out = {"desk": desk,
           "killswitch": killswitch.status(desk, risk_numbers, blocks),
           "hypothesis": _compact(h) if h else None,
           "paper": paper,
           "review": {"date": rv.get("date"), **((rv.get("desks") or {}).get(desk) or {})} if rv else None,
           "counts": {k: sum(1 for x in data.get("hypotheses", []) if x.get("status") == k)
                      for k in ledger.STATUSES},
           "total": len(data.get("hypotheses", []))}
    if full_ledger:
        out["ledger"] = ledger_rows(data)
    return out


def daily_panel(acct) -> dict:
    """For the daily dashboard (it also carries the whole ledger)."""
    try:
        nums = acct.risk_numbers()
    except Exception:
        nums = {"equity": acct.equity(), "peak": acct.peak_equity, "day_start": 0, "open_positions": len(acct.positions)}
    return for_desk("daily", nums, (acct.killswitch or {}).get("blocks", []), paper_daily(acct), full_ledger=True)


def scalp_panel(book, key: str) -> dict:
    from ..scalp.runner import KS_DESK
    desk = KS_DESK.get(key, "scalper_a")
    t = book.last_tick or 0
    from datetime import datetime, timezone
    day = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d") if t else ""
    eq = book.equity()
    nums = {"equity": eq, "peak": max(book.peak_equity or book.history_peak(), eq),
            "day_start": book.day_start.get(day, eq), "open_positions": len(book.positions)}
    return for_desk(desk, nums, (book.killswitch or {}).get("blocks", []), paper_scalp(book))


def safe(fn, *a) -> dict | None:
    """Never let the research panel break a dashboard."""
    try:
        return fn(*a)
    except Exception as e:                       # pragma: no cover - defensive
        return {"error": f"{type(e).__name__}: {e}"}




def inject(page: str) -> str:
    """Put the shared research panel (styles + renderer) into a dashboard page."""
    return page.replace("<!--__RESEARCH__-->", HTML.read_text(encoding="utf-8"))
