"""Backtest like you are trying to kill it.

    1. the written prediction must exist first (locked in the ledger)
    2. the newest part of the history is SEALED: development runs never see it,
       and the sealed run happens once per idea, ever
    3. walk-forward: the same rules scored on consecutive windows; one lucky
       window is a coincidence, not an edge
    4. the Deflated Sharpe Ratio, using every attempt the ledger has counted
    5. a breaker: double costs, delayed fills, and the worst stretches of history

The rules here are fixed (no parameters are fitted), so "walk-forward" means
scoring the unchanged rules on each window in turn. Passing needs most windows
profitable, not one great total.
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from datetime import datetime, timezone

from .. import backtest, fx
from . import stats

MIN_DSR = 0.95
WF_MIN_SHARE = 0.6
HOLDOUT = 0.25          # the newest 25% of the history is sealed


# --- shared -----------------------------------------------------------------------

def _metrics(curve: list[float], trades: list, start: float, periods_per_year: float) -> dict:
    rets = stats.returns([start] + curve)
    sr = stats.sharpe(rets)
    wins = sum(1 for t in trades if _pnl(t) > 0)
    return {"return_pct": round((curve[-1] / start - 1) * 100 if curve else 0.0, 2),
            "max_drawdown_pct": round(stats.max_drawdown([start] + curve) * 100, 2),
            "trades": len(trades), "win_rate_pct": round(100 * wins / len(trades), 1) if trades else 0.0,
            "sharpe": round(sr, 4), "sharpe_ann": round(sr * math.sqrt(periods_per_year), 2),
            "_rets": rets}


def _pnl(t) -> float:
    return t["pnl"] if isinstance(t, dict) else t.pnl


def _check(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "passed": bool(ok), "detail": detail}


def _verdict(h: dict, dev: dict, wf: list, costs2: dict, worst: list, trials: int, trial_sharpes: list,
             breaker: dict | None = None) -> dict:
    from . import ledger
    dsr = stats.deflated_sharpe(dev["_rets"], trials, trial_sharpes)
    dev["dsr"] = round(dsr, 3)
    checks = []
    if h.get("prediction"):
        ok, why = ledger.judge(h, dev)
        checks.append(_check("Beats its own written prediction", ok, "; ".join(why) or "inside every line it drew"))
    elif h.get("status") in ("paper", "passed_paper", "paused"):
        checks.append({"name": "Written prediction", "passed": None,
                       "detail": "none: this strategy reached paper before predictions existed (not counted)"})
    else:
        checks.append(_check("Written prediction", False, "none was written before testing"))
    min_dsr = ((h.get("prediction") or {}).get("fail_if") or {}).get("dsr_below", MIN_DSR)
    checks.append(_check("Deflated Sharpe (luck-adjusted)", dsr >= min_dsr,
                         f"{dsr:.2f} after {trials} attempt{'s' if trials != 1 else ''}; needs {min_dsr:.2f}"))
    active = [w for w in wf if w["trades"]]
    good = sum(1 for w in active if w["return_pct"] > 0)
    checks.append(_check("Walk-forward windows", len(active) >= 3 and good >= WF_MIN_SHARE * len(active),
                         f"{good} of {len(active)} windows with trades were profitable"))
    checks.append(_check("Survives double costs", costs2["return_pct"] > 0,
                         f"{costs2['return_pct']:+.2f}% with 2x spread"))
    if breaker is not None:
        checks.append(_check("Survives delayed fills + double costs", breaker["return_pct"] > 0,
                             f"{breaker['return_pct']:+.2f}%"))
    limit = (h.get("prediction") or {}).get("max_drawdown_pct")
    if worst:
        bad = min(w["return_pct"] for w in worst)
        checks.append(_check("Worst historical stretches", limit is None or -bad <= limit,
                             f"worst of {len(worst)} stretches {bad:+.2f}%"
                             + (f" (prediction allowed -{limit}%)" if limit is not None else "")))
    return {"checks": checks, "passed": all(c["passed"] for c in checks if c["passed"] is not None),
            "dsr": dev["dsr"]}


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


# --- the daily bot ------------------------------------------------------------------

def _combine(curves: dict, start: float) -> list[float]:
    """Several per-instrument accounts (date -> equity) as one account of `start`."""
    dates = sorted({d for c in curves.values() for d in c})
    last = {s: start for s in curves}
    out = []
    for d in dates:
        for s, c in curves.items():
            if d in c:
                last[s] = c[d]
        out.append(sum(last.values()) / len(curves))
    return out


def _run_daily(bars_by_sym: dict, lo: int | None = None, hi: int | None = None, **kw) -> dict:
    """Run every instrument on bars[lo:hi] (lo counted from where trading starts),
    with the 50-bar warm-up taken from just before lo."""
    curves, trades = {}, []
    for sym, bars in bars_by_sym.items():
        a = (lo if lo is not None else fx.WARMUP) - fx.WARMUP
        seg = bars[max(0, a):hi]
        if len(seg) <= fx.WARMUP + 1:
            continue
        res = backtest.run(sym, seg, **kw)
        curves[sym] = {seg[fx.WARMUP + i].date: e for i, e in enumerate(res.equity_curve)}
        trades += res.trades
    start = backtest.Settings().start_equity
    curve = _combine(curves, start) if curves else [start]
    m = _metrics(curve, trades, start, 252)
    if curves:
        m["first"] = min(min(c) for c in curves.values())
        m["last"] = max(max(c) for c in curves.values())
    return m


def _worst_stretches(bars: list, lo: int, hi: int, n: int = 5, size: int = 60) -> list[tuple]:
    """The n non-overlapping `size`-bar stretches where the market itself fell or
    swung the most (largest peak-to-trough move of the closes)."""
    cands = []
    for a in range(lo, max(lo, hi - size), max(1, size // 4)):
        closes = [b.close for b in bars[a:a + size]]
        up = stats.max_drawdown([1 / c for c in closes])      # worst rally against a short
        down = stats.max_drawdown(closes)                      # worst fall against a long
        cands.append((max(up, down), a))
    cands.sort(reverse=True)
    picked: list[int] = []
    for _, a in cands:
        if all(abs(a - p) >= size for p in picked):
            picked.append(a)
        if len(picked) == n:
            break
    return [(a, a + size) for a in sorted(picked)]


def daily(h: dict, bars_by_sym: dict, trials: int, trial_sharpes: list, windows: int = 4,
          engine: str = "offline", strategy=None) -> dict:
    """Development-period test of a daily-bot hypothesis. Never touches the sealed part."""
    n = min(len(b) for b in bars_by_sym.values())
    bars_by_sym = {s: b[-n:] for s, b in bars_by_sym.items()}        # same span for every instrument
    cut = int(n * (1 - HOLDOUT))
    if cut - fx.WARMUP < 120:
        raise SystemExit(f"  only {n} daily bars: need at least ~{int((fx.WARMUP + 120) / (1 - HOLDOUT))}")
    dev = _run_daily(bars_by_sym, fx.WARMUP, cut, engine=engine, strategy=strategy)
    step = (cut - fx.WARMUP) // windows
    wf = []
    for i in range(windows):
        a, b = fx.WARMUP + i * step, (cut if i == windows - 1 else fx.WARMUP + (i + 1) * step)
        w = _clean(_run_daily(bars_by_sym, a, b, engine=engine, strategy=strategy))
        wf.append({k: w.get(k) for k in ("first", "last", "return_pct", "trades", "win_rate_pct", "max_drawdown_pct")})
    costs2 = _clean(_run_daily(bars_by_sym, fx.WARMUP, cut, engine=engine, strategy=strategy, cost_mult=2.0))
    breaker = _clean(_run_daily(bars_by_sym, fx.WARMUP, cut, engine=engine, strategy=strategy, cost_mult=2.0, late_fill=True))
    ref = next(iter(bars_by_sym.values()))
    worst = []
    for a, b in _worst_stretches(ref, fx.WARMUP, cut):
        w = _clean(_run_daily(bars_by_sym, a, b, engine=engine, strategy=strategy))
        worst.append({k: w.get(k) for k in ("first", "last", "return_pct", "trades", "max_drawdown_pct")})
    v = _verdict(h, dev, wf, costs2, worst, trials, trial_sharpes, breaker)
    closes = [b.close for b in ref[fx.WARMUP:cut]]
    return {**_clean(dev), "period": f"{dev.get('first', '?')} → {dev.get('last', '?')}",
            "sealed_from": ref[cut].date, "bars": n, "walk_forward": wf, "costs_2x": costs2,
            "breaker": breaker, "worst_stretches": worst, "regime": stats.regime(closes), **v}


def daily_sealed(h: dict, bars_by_sym: dict, engine: str = "offline", strategy=None) -> dict:
    n = min(len(b) for b in bars_by_sym.values())
    bars_by_sym = {s: b[-n:] for s, b in bars_by_sym.items()}
    cut = int(n * (1 - HOLDOUT))
    res = _clean(_run_daily(bars_by_sym, cut, None, engine=engine, strategy=strategy))
    from . import ledger
    ok, why = ledger.judge(h, res) if h.get("prediction") else (res["return_pct"] > 0, [])
    if res["return_pct"] <= 0:
        ok, why = False, why + [f"lost money ({res['return_pct']:+.2f}%)"]
    closes = [b.close for b in next(iter(bars_by_sym.values()))[cut:]]
    return {**res, "period": f"{res.get('first', '?')} → {res.get('last', '?')}", "passed": ok,
            "why": why, "regime": stats.regime(closes)}


# --- the scalper ----------------------------------------------------------------------

@contextmanager
def _keep_all_trades():
    from ..scalp import book as bk
    old = bk.MAX_TRADES
    bk.MAX_TRADES = 10 ** 9
    try:
        yield
    finally:
        bk.MAX_TRADES = old


def _day(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def _run_scalp(series: dict, cfg, lo: float, hi: float, spread_mult: float = 1.0) -> dict:
    """Replay minutes [lo, hi) (unix seconds). The strategy warms up on the 16 h before lo."""
    from ..scalp import backtest as sb
    warm = 16 * 3600
    seg = {s: [b for b in bars if lo - warm <= b.t < hi] for s, bars in series.items()}
    seg = {s: b for s, b in seg.items() if b}
    if not seg:
        return {"return_pct": 0.0, "trades": 0, "win_rate_pct": 0.0, "max_drawdown_pct": 0.0,
                "sharpe": 0.0, "sharpe_ann": 0.0, "_rets": []}
    with _keep_all_trades():
        book = sb.replay(seg, cfg, spread_mult)
    trades = [t for t in book.trades if datetime.fromisoformat(t["opened"]).timestamp() >= lo]
    days = sorted({_day(x) for x in range(int(lo), int(hi), 86400)})
    by_day = {d: 0.0 for d in days}
    for t in trades:
        by_day[t["closed"][:10]] = by_day.get(t["closed"][:10], 0.0) + t["pnl"]
    eq, curve = book.start_equity, []
    for d in sorted(by_day):
        eq += by_day[d]
        curve.append(eq)
    m = _metrics(curve or [book.start_equity], trades, book.start_equity, 260)
    m["first"], m["last"] = (days[0], days[-1]) if days else ("?", "?")
    return m


def scalper(h: dict, series: dict, cfg, trials: int, trial_sharpes: list, windows: int = 4) -> dict:
    t0 = max(b[0].t for b in series.values())
    t1 = min(b[-1].t for b in series.values()) + 60
    cut = t0 + (t1 - t0) * (1 - HOLDOUT)
    lo = t0 + 16 * 3600                                           # first day is warm-up
    if (cut - lo) / 86400 < 10:
        raise SystemExit("  need at least ~3 weeks of 1-minute history for an honest scalper test")
    dev = _run_scalp(series, cfg, lo, cut)
    step = (cut - lo) / windows
    wf = []
    for i in range(windows):
        w = _clean(_run_scalp(series, cfg, lo + i * step, lo + (i + 1) * step))
        wf.append({k: w.get(k) for k in ("first", "last", "return_pct", "trades", "win_rate_pct", "max_drawdown_pct")})
    costs2 = _clean(_run_scalp(series, cfg, lo, cut, spread_mult=2.0))
    # worst days: the 5 days where the instruments moved the most (high-to-low range)
    rng: dict = {}
    for bars in series.values():
        for b in bars:
            if lo <= b.t < cut:
                d = int(b.t // 86400) * 86400
                hi_, lo_ = rng.get(d, (b.h / b.o, b.l / b.o))
                rng[d] = (max(hi_, b.h / b.o), min(lo_, b.l / b.o))
    hard = sorted(rng, key=lambda d: rng[d][0] - rng[d][1], reverse=True)[:5]
    worst = []
    for d in sorted(hard):
        w = _clean(_run_scalp(series, cfg, d, d + 86400))
        worst.append({k: w.get(k) for k in ("first", "last", "return_pct", "trades", "max_drawdown_pct")})
    v = _verdict(h, dev, wf, costs2, worst, trials, trial_sharpes)
    return {**_clean(dev), "period": f"{dev.get('first')} → {dev.get('last')}",
            "sealed_from": _day(cut), "walk_forward": wf, "costs_2x": costs2, "worst_stretches": worst,
            "regime": "", **v}


def scalper_sealed(h: dict, series: dict, cfg) -> dict:
    t0 = max(b[0].t for b in series.values())
    t1 = min(b[-1].t for b in series.values()) + 60
    cut = t0 + (t1 - t0) * (1 - HOLDOUT)
    res = _clean(_run_scalp(series, cfg, cut, t1))
    from . import ledger
    ok, why = ledger.judge(h, res) if h.get("prediction") else (res["return_pct"] > 0, [])
    if res["return_pct"] <= 0:
        ok, why = False, why + [f"lost money ({res['return_pct']:+.2f}%)"]
    return {**res, "period": f"{res.get('first')} → {res.get('last')}", "passed": ok, "why": why}
