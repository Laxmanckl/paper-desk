"""The weekly critic. Reads what actually happened on paper and says it bluntly.

For each desk:
  - paper results vs the backtest and vs the written prediction
  - what the losing trades of the week had in common
  - mistakes that keep repeating
  - whether the desk should be paused (a recommendation: pausing is done by a
    human, with "enabled": false in config/risk.json)

Writes state/reviews/review-<date>.json and .md, state/reviews/latest.json
(what the dashboards show), and every finding into the ledger.

    python -m jev_bot research review
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone

from .. import journal, killswitch
from . import ledger, panel, stats

WINDOW_DAYS = 7


def _ts(iso: str) -> datetime:
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _metrics(trades: list) -> dict:
    n = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    rs = [t["r"] for t in trades if t.get("r") is not None]
    return {"trades": n, "wins": wins, "win_rate_pct": round(100 * wins / n, 1) if n else 0.0,
            "net": round(sum(t["pnl"] for t in trades), 2),
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None}


def _zones(cond: dict | None) -> dict:
    """Entry conditions as buckets the critic can group losing trades by."""
    c = cond or {}
    z = {}
    if c.get("trend"):
        z["trend"] = c["trend"]
    if c.get("rsi") is not None:
        z["rsi_zone"] = "RSI under 30" if c["rsi"] < 30 else "RSI 30-50" if c["rsi"] < 50 else "RSI 50-70" if c["rsi"] < 70 else "RSI over 70"
    if c.get("spread_x_normal") is not None:
        z["spread_zone"] = "a wider than normal spread" if c["spread_x_normal"] > 1.2 else "a normal spread"
    if c.get("momentum") is not None:
        z["momentum_zone"] = "strong momentum" if abs(c["momentum"]) > 0.5 else "weak momentum"
    return z


def _norm_daily(acct) -> list[dict]:
    out = []
    for t in acct.closed or []:
        risk = abs(t["entry"] - t["stop"]) * t["units"]
        out.append({"symbol": t["symbol"], "side": t["side"], "pnl": t["pnl"], "reason": t.get("reason", ""),
                    "opened": t["opened_at"], "closed": t.get("closed_at", t["opened_at"]),
                    "r": round(t["pnl"] / risk, 2) if risk else None, "regime": t.get("regime") or "",
                    "secs": (_ts(t.get("closed_at", t["opened_at"])) - _ts(t["opened_at"])).total_seconds(),
                    **_zones(t.get("conditions"))})
    return out


def _norm_scalp(book) -> list[dict]:
    return [{"symbol": t["symbol"], "side": t["side"], "pnl": t["pnl"], "reason": t.get("reason", ""),
             "opened": t["opened"], "closed": t["closed"], "r": t.get("r"), "secs": t.get("secs", 0),
             "hour": _ts(t["opened"]).hour, **_zones(t.get("conditions"))} for t in book.trades]


REASON = {"stop": "stop-loss", "target": "target", "time": "time limit", "reverse": "signal flip", "end": "end"}


def _common_ground(week: list, intraday: bool) -> list[str]:
    """Conditions over-represented among the week's losing trades."""
    losers = [t for t in week if t["pnl"] <= 0]
    if len(losers) < 3:
        return []
    found = []
    keys = [("symbol", "on {}"), ("side", "{} trades"), ("reason", "closed by {}")]
    keys += [("hour", "opened {:02d}:00-{:02d}:59 UTC")] if intraday else [("regime", "opened in a {} market")]
    keys += [("trend", "opened when the 1-minute trend was {}"), ("rsi_zone", "entered with {}"),
             ("spread_zone", "entered with {}"), ("momentum_zone", "entered on {}")]
    for key, fmt in keys:
        c_all = Counter(t.get(key) for t in week if t.get(key) not in (None, ""))
        c_los = Counter(t.get(key) for t in losers if t.get(key) not in (None, ""))
        if key == "reason":
            c_los.pop("stop", None)                     # losers closing at their stop says nothing new
        for val, k in c_los.most_common(1):
            share = k / len(losers)
            base = c_all[val] / len(week)
            if share >= 0.5 and k >= 3 and share >= 1.2 * base:
                label = fmt.format(val, val) if key == "hour" else fmt.format(REASON.get(val, val))
                found.append(f"{k} of {len(losers)} losing trades were {label} "
                             f"({share:.0%} of losers vs {base:.0%} of all trades)")
    return found


def _repeats(trades: list, week: list, intraday: bool) -> list[str]:
    out = []
    stops = [t for t in week if t["reason"] == "stop"]
    if intraday and len(stops) >= 5:
        fast = [t for t in stops if t["secs"] < 120]
        if len(fast) / len(stops) >= 0.4:
            out.append(f"{len(fast)} of {len(stops)} stop-outs came within 2 minutes of entry: "
                       "entries are late or the stop sits inside normal noise")
    if len(week) >= 8:
        tl = [t for t in week if t["reason"] == "time"]
        if len(tl) / len(week) >= 0.4:
            out.append(f"{len(tl)} of {len(week)} trades ended on the time limit: the signal is not "
                       "followed through by price")
    rev = [t for t in week if t["reason"] == "reverse"]
    if len(rev) >= 2 and sum(t["pnl"] for t in rev) < 0:
        out.append(f"{len(rev)} positions were closed by an opposite signal at a net "
                   f"${sum(t['pnl'] for t in rev):,.2f}: the signal is flipping back and forth")
    streak, worst, sym = 0, 0, ""
    for t in sorted(trades, key=lambda x: x["closed"])[-50:]:
        streak = streak + 1 if t["pnl"] <= 0 else 0
        if streak > worst:
            worst, sym = streak, t["symbol"]
    if worst >= 5:
        out.append(f"a run of {worst} losing trades in a row (latest on {sym})")
    return out


def _journal_findings(entries: list) -> list[str]:
    """What the decision journal says about the signals that did NOT become trades."""
    out = []
    skipped = [e for e in entries if e.get("decision") in ("SKIP", "BLOCKED") and e.get("side")]
    if len(skipped) >= 3:
        why = Counter((e.get("reasoning") or "").split("not traded: ")[-1].split(";")[0][:70] for e in skipped)
        top, k = why.most_common(1)[0]
        out.append(f"{len(skipped)} signals were not traded this week; the most common reason ({k}x): {top}")
    return out


def desk_review(desk: str, trades: list, paper: dict, ks: dict, h: dict | None, now: datetime,
                intraday: bool, entries: list | None = None) -> dict:
    since = now - timedelta(days=WINDOW_DAYS)
    week = [t for t in trades if _ts(t["closed"]) >= since]
    wk, life = _metrics(week), _metrics(trades)
    findings, cmp = [], []
    bt = (h or {}).get("backtest")
    if bt and bt.get("trades"):
        bt_wr = bt.get("win_rate_pct", 0)
        if life["trades"] >= 10:
            gap = life["win_rate_pct"] - bt_wr
            cmp.append(f"win rate on paper {life['win_rate_pct']:.0f}% vs {bt_wr:.0f}% in the backtest "
                       f"({gap:+.0f} pts)")
            if gap <= -10:
                findings.append(f"performing worse live than in testing: win rate {gap:+.0f} pts below the backtest")
        if not bt.get("passed"):
            findings.append("its last honest backtest did not pass ("
                            + ", ".join(c["name"].lower() for c in bt.get("checks", []) if not c["passed"]) + ")")
    else:
        cmp.append("no honest backtest yet: nothing to compare paper results against")
    pred = (h or {}).get("prediction")
    if pred:
        ok, why = ledger.judge(h, paper)
        if pred.get("scope"):
            cmp.append("forward prediction: " + ("on track" if ok else "missing it: " + "; ".join(why)))
        elif not ok and paper["trades"] >= pred.get("min_trades", 0):
            findings.append("paper results break the written prediction: " + "; ".join(why))
    else:
        cmp.append("no written prediction yet")
    findings += _common_ground(week, intraday)
    findings += _repeats(trades, week, intraday)
    findings += _journal_findings(entries or [])
    week_blocks = [b for b in ks.get("blocks", []) if _ts(b[0]) >= since]
    if week_blocks:
        findings.append(f"the kill switch blocked {len(week_blocks)} trade(s) this week: latest '{week_blocks[-1][1]}'")
    dd = next(m for m in ks["meters"] if m["key"] == "drawdown")

    pause_why = []
    if ks["state"] == "halted":
        pause_why.append("the kill switch has halted it")
    if dd["frac"] >= 0.75:
        pause_why.append(f"it is {dd['value']:.1f}% below its peak, {dd['frac']:.0%} of the way to its limit")
    if life["trades"] >= 20 and (life["avg_r"] or 0) < 0 and (not bt or life["win_rate_pct"] < bt.get("win_rate_pct", 0) - 10):
        pause_why.append(f"{life['trades']} trades on paper and still losing on average ({life['avg_r']}R per trade)")
    if bt and not bt.get("passed") and life["net"] < 0 and life["trades"] >= 10:
        pause_why.append("it failed its honest backtest and is losing on paper too")
    recommend = "pause" if len(pause_why) >= 1 and (ks["state"] == "halted" or len(pause_why) >= 2) else (
        "watch" if pause_why or findings else "keep")
    if not trades:
        recommend, findings = "keep", ["no closed trades yet: nothing to judge"]
    return {"name": killswitch.name(desk), "hypothesis": (h or {}).get("id"),
            "week": wk, "lifetime": life, "paper": paper, "compare": cmp, "findings": findings,
            "recommend": recommend, "recommend_why": pause_why,
            "regime": paper.get("regime", "")}


def run(daily_account: str | None, scalp_accounts: dict, out_dir: str | None = None,
        now: datetime | None = None, write: bool = True) -> dict:
    """scalp_accounts: {"a": path, "orb": path}. Missing files are skipped."""
    from .. import live
    from ..scalp.book import ScalpBook
    from ..scalp.runner import KS_DESK
    now = now or datetime.now(timezone.utc)
    data = ledger.load()
    desks = {}
    regimes = {}
    if daily_account and os.path.exists(daily_account):
        acct = live.Account.load(daily_account)
        for sym, cl in (acct.closes or {}).items():
            regimes[sym] = stats.regime([c[1] for c in cl])
        paper = panel.paper_daily(acct)
        paper["regime"] = ", ".join(f"{s} {r}" for s, r in regimes.items())
        ks = killswitch.status("daily", acct.risk_numbers(now), (acct.killswitch or {}).get("blocks", []))
        since = now - timedelta(days=WINDOW_DAYS)
        desks["daily"] = desk_review("daily", _norm_daily(acct), paper, ks, panel.primary(data, "daily"), now, False,
                                     journal.read_since("daily", since))
        # strategies promoted to paper, each on its own account
        for h in data["hypotheses"]:
            p = os.path.join(os.path.dirname(daily_account) or ".", "strategies", f"{h['id']}.json")
            if h.get("status") != "paper" or not h.get("module") or not os.path.exists(p):
                continue
            sa = live.Account.load(p)
            desk = f"daily:{h['id']}"
            sp = panel.paper_daily(sa)
            sks = killswitch.status(desk, sa.risk_numbers(now, desk=desk), (sa.killswitch or {}).get("blocks", []))
            desks[desk] = desk_review(desk, _norm_daily(sa), sp, sks, h, now, False, journal.read_since(desk, since))
    for key, path in scalp_accounts.items():
        if not path or not os.path.exists(path):
            continue
        book = ScalpBook.load(path)
        desk = KS_DESK.get(key, key)
        paper = panel.paper_scalp(book)
        paper["regime"] = ", ".join(f"{s} {r}" for s, r in regimes.items())
        day = now.strftime("%Y-%m-%d")
        ks = killswitch.status(desk, {"equity": book.equity(), "peak": max(book.peak_equity or book.history_peak(), book.equity()),
                                      "day_start": book.day_start.get(day, book.equity()),
                                      "open_positions": len(book.positions)},
                               (book.killswitch or {}).get("blocks", []))
        desks[desk] = desk_review(desk, _norm_scalp(book), paper, ks, panel.primary(data, desk), now, True,
                                  journal.read_since(desk, now - timedelta(days=WINDOW_DAYS)))
    review = {"date": now.strftime("%Y-%m-%d"), "at": now.isoformat(timespec="seconds"),
              "window_days": WINDOW_DAYS, "desks": desks}
    if not write:
        return review
    stamp = f"critic {review['date']}"
    for desk, r in desks.items():
        h = ledger.get(data, desk.split(":", 1)[1]) if ":" in desk else panel.primary(data, desk)
        if not h:
            continue
        h["paper"] = {**r["paper"], "at": review["at"]}
        if r["paper"].get("regime"):
            h["regime"] = r["paper"]["regime"]
        for f in r["findings"]:
            ledger.add_lesson(h, f"{f}", stamp)
        if r["recommend"] == "pause":
            ledger.add_lesson(h, "critic recommends pausing: " + "; ".join(r["recommend_why"]), stamp)
    ledger.save(data)
    out = out_dir or str(panel.REVIEW_DIR)
    os.makedirs(out, exist_ok=True)
    for name in (f"review-{review['date']}.json", "latest.json"):
        with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
            json.dump(review, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    with open(os.path.join(out, f"review-{review['date']}.md"), "w", encoding="utf-8") as fh:
        fh.write(markdown(review))
    return review


def markdown(review: dict) -> str:
    L = [f"# Weekly critic · {review['date']}", "",
         f"Paper trades from the last {review['window_days']} days, read bluntly. "
         "Pausing is a human decision: set `\"enabled\": false` for the desk in `config/risk.json`.", ""]
    for desk, r in review["desks"].items():
        w, life = r["week"], r["lifetime"]
        L += [f"## {r['name']} · recommendation: **{r['recommend'].upper()}**", "",
              f"- This week: {w['trades']} trades, {w['win_rate_pct']:.0f}% won, net ${w['net']:,.2f}",
              f"- Since the start: {life['trades']} trades, {life['win_rate_pct']:.0f}% won, net ${life['net']:,.2f}"
              + (f", {life['avg_r']}R per trade" if life["avg_r"] is not None else "")]
        L += [f"- {c}" for c in r["compare"]]
        L += ["", "**Findings**", ""] + [f"- {f}" for f in r["findings"] or ["nothing notable"]]
        if r["recommend_why"]:
            L += ["", "**Why pause**" if r["recommend"] == "pause" else "**Watch points**", ""]
            L += [f"- {x}" for x in r["recommend_why"]]
        L.append("")
    return "\n".join(L)
