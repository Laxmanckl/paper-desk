"""Scalper backtest on 1-minute candles, with the same engine the live bot uses.

Each candle is replayed as four prices (open, then the nearer extreme, the
other extreme, close) with a spread around them, so stops and targets are
hit in a realistic order. Costs are reported separately so you can see what
the strategy makes BEFORE costs and how much the spread and fees take.

    python -m jev_bot scalp-backtest                       # simulated BTC, EUR/USD, gold
    python -m jev_bot scalp-backtest --sweep 20            # 20 simulated months
    python -m jev_bot scalp-backtest --sweep 20 --trendless   # control: no trends at all
    python -m jev_bot scalp-backtest BTCUSDT --csv BTCUSDT=btc_1m.csv
"""

from __future__ import annotations

import csv
import random
from datetime import datetime, timezone

from . import instruments
from .bars import Bar
from .book import ScalpBook
from .engine import Engine
from .strategy import ScalpConfig

# typical quiet-market spread, used when the data has none (price units)
DEFAULT_SPREAD = {"crypto": 0.0002, "EURUSD": 0.00008, "GBPUSD": 0.00012, "USDJPY": 0.010,
                  "XAUUSD": 0.25}
SIM = {"BTCUSDT": (62000, 0.0007), "ETHUSDT": (2400, 0.0009), "SOLUSDT": (150, 0.0013),
       "EURUSD": (1.12, 0.00012), "GBPUSD": (1.30, 0.00015), "USDJPY": (148.0, 0.00014),
       "XAUUSD": (4170, 0.00030)}
T0 = 1791158400            # Mon 2026-10-05 00:00 UTC


def spread_for(sym: str, price: float) -> float:
    spec = instruments.get(sym)
    if spec.kind == "crypto":
        return price * DEFAULT_SPREAD["crypto"]
    return DEFAULT_SPREAD.get(sym, 0.00012 if not sym.endswith("JPY") else 0.012)


def simulate(sym: str, days: int, seed: int, trends: bool = True) -> list[Bar]:
    """1-minute candles: random walk with trending stretches (or none, as a control)."""
    start, vol = SIM.get(sym, (100.0, 0.0008))
    rng = random.Random(f"{sym}-{seed}-{trends}")
    p, out, drift, left = start, [], 0.0, 0
    for i in range(days * 1440):
        if left <= 0:
            drift = (rng.choice([-1, 0, 0, 1]) * vol * 0.12) if trends else 0.0
            left = rng.randint(30, 240)
        left -= 1
        o = p
        steps = [o]
        for _ in range(4):
            steps.append(steps[-1] * (1 + rng.gauss(drift / 4, vol / 2)))
        c = steps[-1]
        out.append(Bar(T0 + i * 60, o, max(steps), min(steps), c))
        p = c
    return out


def load_csv(path: str) -> list[Bar]:
    """time (unix s/ms or ISO), open, high, low, close[, spread]. Header row required."""
    out = []
    with open(path, newline="") as fh:
        r = csv.reader(fh)
        head = [h.strip().lower() for h in next(r)]
        ix = {k: head.index(k) for k in ("open", "high", "low", "close") if k in head}
        ti = next((head.index(k) for k in ("time", "timestamp", "open_time", "date", "datetime") if k in head), 0)
        for row in r:
            if not row:
                continue
            raw = row[ti].strip()
            try:
                t = float(raw)
                t = t / 1000 if t > 1e11 else t
            except ValueError:
                t = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp()
            out.append(Bar(int(t), *(float(row[ix[k]]) for k in ("open", "high", "low", "close"))))
    return sorted(out, key=lambda b: b.t)


def replay(series: dict, cfg: ScalpConfig) -> ScalpBook:
    """series: symbol -> list[Bar] (same minutes). Returns the finished paper book."""
    book = ScalpBook(created=datetime.fromtimestamp(T0, timezone.utc).isoformat())
    eng = Engine(book, list(series), cfg)
    n = min(len(b) for b in series.values())
    for i in range(n):
        for sym, bars in series.items():
            b = bars[i]
            path = [b.o, b.l, b.h, b.c] if (b.h - b.o) > (b.o - b.l) else [b.o, b.h, b.l, b.c]
            for k, price in enumerate(path):
                half = spread_for(sym, price) / 2
                eng.on_tick(sym, price - half, price + half, b.t + (0, 15, 30, 59)[k])
    last_t = max(bars[n - 1].t for bars in series.values()) + 60
    for sym in list(book.positions):
        q = book.prices[sym]
        book.close(sym, q["bid"] if book.positions[sym]["side"] == "BUY" else q["ask"], last_t, "end")
    return book


def summary(book: ScalpBook) -> dict:
    s = book.stats.get("_all", {"n": 0, "wins": 0, "fees": 0, "pnl": 0, "gross_win": 0, "gross_loss": 0})
    gross = sum(t["gross"] for t in book.trades)
    peak, mdd, eq = book.start_equity, 0.0, book.start_equity
    for t in book.trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    pf = s["gross_win"] / -s["gross_loss"] if s["gross_loss"] else float("inf") if s["gross_win"] else 0
    return {"trades": s["n"], "win_rate": s["wins"] / s["n"] if s["n"] else 0, "net": s["pnl"],
            "fees": s["fees"], "gross": gross, "pf": pf, "max_dd": mdd,
            "ret": book.cash / book.start_equity - 1}


def report(name: str, book: ScalpBook) -> str:
    m = summary(book)
    by = {k: v for k, v in book.stats.items() if k != "_all"}
    pf = "inf" if m["pf"] == float("inf") else f"{m['pf']:.2f}"
    lines = ["=" * 66, f"  SCALP BACKTEST · {name}", "-" * 66,
             f"  trades {m['trades']:>6}   win rate {m['win_rate']:.0%}   profit factor {pf}",
             f"  net    ${m['net']:>+10,.2f}  ({m['ret']:+.2%})   max drawdown {m['max_dd']:.2%}",
             f"  before fees ${m['net'] + m['fees']:>+10,.2f}   fees ${m['fees']:,.2f}   "
             f"(the spread is already inside every fill)", "-" * 66]
    for k, v in sorted(by.items(), key=lambda kv: -kv[1]["pnl"]):
        lines.append(f"    {k:<9} {v['n']:>5} trades  {v['wins'] / v['n']:>4.0%} won  net ${v['pnl']:>+9,.2f}  "
                     f"fees ${v['fees']:>8,.2f}")
    lines.append("=" * 66)
    return "\n".join(lines)


def cli(a) -> None:
    cfg = ScalpConfig(crypto_fee=a.crypto_fee, fx_commission_per_100k=a.fx_commission)
    syms = [instruments.get(s).symbol for s in (a.symbols or ["BTCUSDT", "EURUSD", "XAUUSD"])]
    csvs = dict(c.split("=", 1) for c in a.csv)
    csvs = {instruments.get(k).symbol: v for k, v in csvs.items()}
    if a.sweep:
        rows = []
        for seed in range(1, a.sweep + 1):
            series = {s: simulate(s, a.days, seed, not a.trendless) for s in syms}
            rows.append(summary(replay(series, cfg)))
        rets = sorted(r["ret"] for r in rows)
        kind = "random-walk (no trends)" if a.trendless else "simulated"
        print(f"  SWEEP · {a.sweep} {kind} runs of {a.days} days · {', '.join(syms)}")
        print("  median {:+.2%} · worst {:+.2%} · best {:+.2%} · profitable in {}/{} · "
              "avg trades {:.0f} · avg fees ${:,.0f} · avg before-fees {:+.2%}".format(
                  rets[len(rets) // 2], rets[0], rets[-1], sum(r > 0 for r in rets), len(rets),
                  sum(r["trades"] for r in rows) / len(rows), sum(r["fees"] for r in rows) / len(rows),
                  sum((r["net"] + r["fees"]) for r in rows) / len(rows) / 10_000))
        return
    series = {s: load_csv(csvs[s]) if s in csvs else simulate(s, a.days, 7, not a.trendless) for s in syms}
    name = ", ".join(f"{s} ({'csv' if s in csvs else 'sim'})" for s in syms) + f" · {a.days} days"
    print(report(name, replay(series, cfg)))
