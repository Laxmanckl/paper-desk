"""Guide Step 8, "Test before you trust it", computed from what the desk logs.

  1. Does the whole system beat simpler versions?
     Jev + confidence gate (live account) vs code-only rules (shadow) vs Jev
     without the gate (shadow). Same data, same costs, same risk limits.
  2. Is Jev calibrated on your market?
     When Jev said 70%, did it happen ~70% of the time? Direction is checked
     against where the price actually was one hour later; "move continues"
     likewise; "costs abnormal" against the spread at that moment.
  3. Do the fees eat you alive?
     Spread + commission per trade vs what the average trade makes before costs.

Each verdict stays "not enough data" until the sample is big enough. The guide
asks for at least two weeks of paper trading before any real money.

    python -m jevsys report      prints it; the desk also refreshes it hourly for the dashboard
"""
from __future__ import annotations

import bisect
import glob
import gzip
import json
import os
import time

MIN_TRADES = 20
MIN_CALLS = 100
PAPER_DAYS = 14
BINS = [0.0, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01]


def read_logs(log_dir: str, since: float = 0.0) -> list[dict]:
    rows = []
    for path in sorted(glob.glob(os.path.join(log_dir, "snapshots-*.jsonl*"))):
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue  # a half-written last line after a crash
                    if r.get("ts", 0) >= since and r.get("sym") and r.get("snap"):
                        rows.append(r)
        except OSError:
            continue
    rows.sort(key=lambda r: r["ts"])
    return rows


def _bins(pairs: list[tuple[float, bool]]) -> dict:
    """pairs of (predicted probability, happened?) -> calibration table + expected calibration error."""
    table = []
    for lo, hi in zip(BINS, BINS[1:]):
        pts = [(p, y) for p, y in pairs if lo <= p < hi]
        if pts:
            table.append({"lo": lo, "hi": min(hi, 1.0), "n": len(pts), "predicted": round(sum(p for p, _ in pts) / len(pts), 3),
                          "actual": round(sum(1 for _, y in pts if y) / len(pts), 3)})
    n = sum(b["n"] for b in table)
    ece = sum(b["n"] * abs(b["predicted"] - b["actual"]) for b in table) / n if n else None
    return {"n": n, "bins": table, "error": round(ece, 3) if ece is not None else None}


def calibration(rows: list[dict], horizon_s: float = 3600) -> dict:
    by_sym: dict[str, tuple[list[float], list[float]]] = {}
    for r in rows:
        if r.get("fresh") and r["snap"].get("mid"):
            ts, mids = by_sym.setdefault(r["sym"], ([], []))
            ts.append(r["ts"])
            mids.append(r["snap"]["mid"])

    def future_mid(sym, t):
        ts, mids = by_sym.get(sym, ([], []))
        i = bisect.bisect_left(ts, t + horizon_s - 60)
        if i < len(ts) and ts[i] <= t + horizon_s + 900:
            return mids[i]
        return None

    direction, cont, costs = [], [], []
    for r in rows:
        j = r.get("jev")
        if not j or not r.get("fresh"):
            continue
        snap = r["snap"]
        ca = (j.get("costs_abnormal") or {}).get("p")
        if ca is not None and snap.get("spread_vs_norm") is not None:
            costs.append((ca, snap["spread_vs_norm"] >= 1.5))
        fut = future_mid(r["sym"], r["ts"])
        if fut is None or fut == snap["mid"]:
            continue
        went_up = fut > snap["mid"]
        d = j.get("direction") or {}
        if d.get("value") in ("up", "down") and d.get("p") is not None:
            direction.append((d["p"], went_up == (d["value"] == "up")))
        mc = (j.get("move_continues") or {}).get("p")
        r1h = snap.get("ret_1h_bps")
        if mc is not None and r1h:
            cont.append((mc, went_up == (r1h > 0)))
    return {"direction": _bins(direction), "move_continues": _bins(cont), "costs_abnormal": _bins(costs),
            "horizon_min": horizon_s / 60}


def _max_dd(points: list) -> float:
    peak, dd = None, 0.0
    for _, eq in points:
        peak = eq if peak is None else max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100 if peak else 0)
    return round(dd, 2)


def account(trader) -> dict:
    b = trader.book
    st = b.stats()
    tr = b.trades
    cost = sum(t.get("spread_cost", 0) + t.get("fees", 0) for t in tr)
    gross = sum(t["pnl"] for t in tr) + cost
    return {"name": trader.name, "label": trader.label, "trades": st["n"], "win_rate": st["win_rate"],
            "profit_factor": None if st["profit_factor"] in (None, float("inf")) else round(st["profit_factor"], 2),
            "net": st["net"], "avg_r": None if st["avg_r"] is None else round(st["avg_r"], 3),
            "max_drawdown_pct": _max_dd(b.equity_pts), "open": len(b.positions),
            "avg_cost": round(cost / len(tr), 2) if tr else None,
            "avg_gross": round(gross / len(tr), 2) if tr else None,
            "avg_net": round(st["net"] / len(tr), 2) if tr else None,
            "halted": bool(b.halted)}


def build(log_dir: str, traders: list, created: float, now: float | None = None) -> dict:
    now = now or time.time()
    rows = read_logs(log_dir, since=now - 60 * 86_400)
    cal = calibration(rows)
    accts = {t.name: account(t) for t in traders}
    days = max(0.0, (now - created) / 86_400)
    jev, rules, nogate = accts.get("jev"), accts.get("rules"), accts.get("nogate")

    def enough(*a):
        return all(x and x["trades"] >= MIN_TRADES for x in a)

    checks = []
    if enough(jev, rules):
        ok = jev["net"] > rules["net"]
        checks.append({"key": "beats_rules", "status": "pass" if ok else "fail",
                       "text": f"Jev system ${jev['net']:+,.2f} vs code-only rules ${rules['net']:+,.2f}"})
    else:
        checks.append({"key": "beats_rules", "status": "wait",
                       "text": f"needs {MIN_TRADES}+ closed trades in both accounts (now {jev and jev['trades']} / {rules and rules['trades']})"})
    if enough(jev, nogate):
        ok = jev["net"] > nogate["net"]
        checks.append({"key": "gate_helps", "status": "pass" if ok else "fail",
                       "text": f"with confidence gate ${jev['net']:+,.2f} vs without ${nogate['net']:+,.2f}"})
    else:
        checks.append({"key": "gate_helps", "status": "wait",
                       "text": f"needs {MIN_TRADES}+ closed trades with and without the gate (now {jev and jev['trades']} / {nogate and nogate['trades']})"})
    dcal = cal["direction"]
    if dcal["n"] >= MIN_CALLS:
        ok = dcal["error"] <= 0.10
        checks.append({"key": "calibrated", "status": "pass" if ok else "fail",
                       "text": f"direction calls off by {dcal['error']:.0%} on average over {dcal['n']} calls (pass: 10% or less)"})
    else:
        checks.append({"key": "calibrated", "status": "wait",
                       "text": f"needs {MIN_CALLS}+ up/down calls with a known outcome (now {dcal['n']})"})
    if enough(jev):
        ok = jev["avg_net"] > 0 and jev["avg_gross"] > jev["avg_cost"]
        checks.append({"key": "fees", "status": "pass" if ok else "fail",
                       "text": f"avg trade makes ${jev['avg_gross']:,.2f} before costs, pays ${jev['avg_cost']:,.2f} in spread/fees, nets ${jev['avg_net']:+,.2f}"})
    else:
        checks.append({"key": "fees", "status": "wait", "text": f"needs {MIN_TRADES}+ closed trades (now {jev and jev['trades']})"})
    checks.append({"key": "two_weeks", "status": "pass" if days >= PAPER_DAYS else "wait",
                   "text": f"day {days:.1f} of {PAPER_DAYS} of paper trading"})
    ready = all(c["status"] == "pass" for c in checks)
    return {"generated": now, "days": round(days, 2), "accounts": accts, "calibration": cal, "checks": checks,
            "ready_for_real_money": ready, "log_rows": len(rows)}


def to_text(r: dict) -> str:
    out = [f"Jev Desk · Step 8 report · {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(r['generated']))}",
           f"Paper trading day {r['days']:.1f} of {PAPER_DAYS}", "", "Accounts (same data, same costs, same limits):"]
    for a in r["accounts"].values():
        wr = "–" if a["win_rate"] is None else f"{a['win_rate']:.0%}"
        out.append(f"  {a['label']:<34} trades {a['trades']:>4}  win {wr:>4}  net ${a['net']:>+10,.2f}  "
                   f"PF {a['profit_factor'] if a['profit_factor'] is not None else '–':>5}  max DD {a['max_drawdown_pct']:.2f}%")
    for name, label in [("direction", "Direction (vs price 1 h later)"), ("move_continues", "Move continues (1 h)"),
                        ("costs_abnormal", "Costs abnormal (spread >= 1.5x normal)")]:
        c = r["calibration"][name]
        out += ["", f"Calibration · {label}: {c['n']} calls" + (f", average error {c['error']:.0%}" if c["error"] is not None else "")]
        for b in c["bins"]:
            out.append(f"  said {b['lo']:.0%}-{b['hi']:.0%}: n={b['n']:>4}  predicted {b['predicted']:.0%}  happened {b['actual']:.0%}")
    out += ["", "Checks:"]
    for c in r["checks"]:
        out.append(f"  [{c['status'].upper():>4}] {c['key']}: {c['text']}")
    out += ["", "READY for a small real-money test." if r["ready_for_real_money"]
            else "NOT ready for real money: every check must pass first (guide Step 8)."]
    return "\n".join(out)
