"""The hypothesis ledger: every idea ever tested, what happened, and in what market.

Failed ideas are the most valuable records here. Before proposing anything new,
check the ledger (`python -m jev_bot research search ...`) so the same failed
idea is not rediscovered every week.

Stored as JSON in state/ledger.json (not SQLite) so it diffs in git, needs no
dependency, and both dashboards can embed it directly.

One hypothesis:
    id, idea, desk, markets, source, created
    strategy        which code runs it, and its version
    variations      how many versions have been tested (raises the luck bar)
    max_variations  rewrite cap (default 5); a strategy that needs more has failed
    falsify         what result would prove it wrong (written up front)
    prediction      written BEFORE the first test, then locked (sha256 + time)
    regime          market regime during testing
    backtest        latest development-period result (walk-forward, stress, DSR)
    attempts        every backtest attempt, kept forever
    sealed          the one run on the sealed holdout period (null until used)
    paper           latest paper-trading snapshot (from the weekly review)
    status          see STATUSES
    lessons         what was learned, newest last
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent.parent / "state" / "ledger.json"

STATUSES = {
    "untested": "Untested",
    "testing": "Backtesting",
    "failed": "Failed",
    "passed_backtest": "Passed backtest only",
    "passed_sealed": "Passed sealed test",
    "paper": "Paper trading",
    "passed_paper": "Passed paper",
    "paused": "Paused",
    "retired": "Retired",
}
REGIMES = ("trending up", "trending down", "choppy", "high volatility")
DESKS = ("daily", "scalper_a", "scalper_orb", "scalper_hiwin", "idea")
MAX_ATTEMPTS_KEPT = 200


class LedgerError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: str | os.PathLike | None = None) -> dict:
    p = Path(path or PATH)
    if not p.exists():
        return {"version": 1, "hypotheses": []}
    with open(p, encoding="utf-8") as fh:
        data = json.load(fh)
    data.setdefault("hypotheses", [])
    return data


def save(data: dict, path: str | os.PathLike | None = None) -> None:
    p = Path(path or PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(p) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, p)


def get(data: dict, hid: str) -> dict:
    for h in data["hypotheses"]:
        if h["id"].lower() == hid.lower():
            return h
    raise LedgerError(f"no hypothesis {hid!r} in the ledger")


def for_desk(data: dict, desk: str) -> list[dict]:
    return [h for h in data["hypotheses"] if h.get("desk") == desk]


def add(data: dict, idea: str, desk: str = "idea", markets: list | None = None, source: str = "you",
        falsify: str = "", strategy: str = "", status: str = "untested", note: str = "") -> dict:
    if desk not in DESKS:
        raise LedgerError(f"desk must be one of {', '.join(DESKS)}")
    n = 1 + max((int(h["id"][1:]) for h in data["hypotheses"] if h["id"][1:].isdigit()), default=0)
    h = {"id": f"H{n}", "idea": idea.strip(), "desk": desk, "markets": markets or [], "source": source,
         "created": _now(), "strategy": strategy, "version": 1, "variations": 0, "max_variations": 5,
         "falsify": falsify, "prediction": None, "regime": "", "backtest": None, "attempts": [],
         "sealed": None, "paper": None, "status": status, "lessons": [], "notes": note, "module": ""}
    data["hypotheses"].append(h)
    return h


# --- the written prediction, locked before the first test ------------------------

PREDICTION_KEYS = ("return_pct", "win_rate_pct", "max_drawdown_pct", "min_trades", "fail_if")


def _digest(pred: dict) -> str:
    body = {k: pred.get(k) for k in PREDICTION_KEYS + ("written",)}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def predict(h: dict, return_pct: float, win_rate_pct: float, max_drawdown_pct: float,
            min_trades: int, fail_if: dict) -> dict:
    """Write the prediction. Only once per idea: it can never be edited afterwards."""
    if h.get("prediction"):
        raise LedgerError(f"{h['id']} already has a prediction (locked {h['prediction']['written']}). "
                          "Predictions are never edited; that is the point.")
    if h.get("attempts") and h.get("status") not in ("paper",):
        raise LedgerError(f"{h['id']} has already been backtested; a prediction written after "
                          "seeing results does not count. Add a new hypothesis instead.")
    pred = {"return_pct": return_pct, "win_rate_pct": win_rate_pct, "max_drawdown_pct": max_drawdown_pct,
            "min_trades": min_trades, "fail_if": fail_if, "written": _now()}
    if h.get("status") == "paper":
        pred["scope"] = "forward paper trading from the date written (this strategy reached paper before predictions existed)"
    pred["sha256"] = _digest(pred)
    h["prediction"] = pred
    return pred


def prediction_intact(h: dict) -> bool | None:
    p = h.get("prediction")
    if not p:
        return None
    return p.get("sha256") == _digest(p)


def judge(h: dict, result: dict) -> tuple[bool, list[str]]:
    """Did a result clear the prediction's own failure lines? (passed, reasons it failed)"""
    p = h.get("prediction") or {}
    f = p.get("fail_if") or {}
    why = []
    if "return_below" in f and result.get("return_pct", 0) < f["return_below"]:
        why.append(f"return {result.get('return_pct', 0):+.2f}% < {f['return_below']}%")
    if "drawdown_above" in f and result.get("max_drawdown_pct", 0) > f["drawdown_above"]:
        why.append(f"drawdown {result.get('max_drawdown_pct', 0):.2f}% > {f['drawdown_above']}%")
    if "win_rate_below" in f and result.get("win_rate_pct", 0) < f["win_rate_below"]:
        why.append(f"win rate {result.get('win_rate_pct', 0):.0f}% < {f['win_rate_below']}%")
    if result.get("trades", 0) < p.get("min_trades", 0):
        why.append(f"only {result.get('trades', 0)} trades, prediction needed {p.get('min_trades')}")
    return not why, why


# --- attempts, the sealed run, revisions ------------------------------------------

def record_attempt(h: dict, result: dict) -> None:
    """Every development-period backtest counts as an attempt, pass or fail."""
    h["variations"] = max(h.get("variations", 0), len(h.get("attempts", [])) + 1)
    entry = {"at": _now(), "version": h.get("version", 1), **{k: result.get(k) for k in (
        "return_pct", "win_rate_pct", "max_drawdown_pct", "trades", "sharpe_ann", "sharpe", "dsr",
        "data", "passed")}}
    h.setdefault("attempts", []).append(entry)
    del h["attempts"][:-MAX_ATTEMPTS_KEPT]
    h["backtest"] = {**result, "at": entry["at"], "version": h.get("version", 1)}
    if result.get("regime"):
        h["regime"] = result["regime"]


def revise(h: dict, note: str) -> None:
    """A rewrite of the strategy after a failed backtest: one more version, one more
    attempt counted, and capped. The sealed period is never touched by revisions."""
    if h.get("sealed"):
        raise LedgerError(f"{h['id']} has used its sealed test; revising it now would be fitting to "
                          "the holdout. Add a new hypothesis instead.")
    cap = h.get("max_variations", 5)
    if h.get("variations", 0) >= cap:
        h["status"] = "failed"
        add_lesson(h, f"hit the rewrite cap ({cap} versions) without passing; marked failed", "ledger")
        raise LedgerError(f"{h['id']} has had {cap} versions tested. A strategy that needs more "
                          "has told you what you need to know: marked failed.")
    h["version"] = h.get("version", 1) + 1
    h["status"] = "testing"
    add_lesson(h, f"v{h['version']}: {note}", "revision")


def record_sealed(h: dict, result: dict) -> None:
    if h.get("sealed"):
        raise LedgerError(f"{h['id']} already used its sealed test on {h['sealed']['at']}. "
                          "It gets exactly one.")
    h["sealed"] = {**result, "at": _now(), "version": h.get("version", 1)}


def add_lesson(h: dict, text: str, source: str = "you") -> bool:
    text = text.strip()
    if not text or any(x["text"] == text for x in h.get("lessons", [])):
        return False
    h.setdefault("lessons", []).append({"at": _now(), "source": source, "text": text})
    del h["lessons"][:-30]
    return True


def set_status(h: dict, status: str) -> None:
    if status not in STATUSES:
        raise LedgerError(f"status must be one of {', '.join(STATUSES)}")
    h["status"] = status


def search(data: dict, market: str = "", regime: str = "", status: str = "", desk: str = "",
           text: str = "") -> list[dict]:
    """What research agents run before proposing anything."""
    out = []
    for h in data["hypotheses"]:
        if market and market.upper() not in [m.upper() for m in h.get("markets", [])]:
            continue
        if regime and regime.lower() not in (h.get("regime") or "").lower():
            continue
        if status and h.get("status") != status:
            continue
        if desk and h.get("desk") != desk:
            continue
        if text and text.lower() not in (h.get("idea", "") + " " + " ".join(
                x["text"] for x in h.get("lessons", []))).lower():
            continue
        out.append(h)
    return out
