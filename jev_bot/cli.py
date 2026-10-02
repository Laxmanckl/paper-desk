"""reflex command line.

    reflex decisions        one cycle: state -> JEV -> risk, show the table
    reflex run              ... and execute the approved ones on paper
    reflex card SYMBOL      unpack a single decision
    reflex backtest         walk XAUUSD / EURUSD history on paper (forex + gold)
    reflex live             paper-trade XAUUSD / EURUSD on real live prices
    reflex account          show the live paper account
    reflex dashboard        build the dashboard web page from the account

Add --market fx to decisions / run / card for gold and EUR/USD.

Offline decision engine by default (no key, reproducible). Pass
--engine jev with TYPESAFE_API_KEY set to use the real TypeSafe model.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import render, markets, fx, backtest, feeds, live, dashboard
from .loop import run
from .risk import Limits
from .execution.paper import Book


def _csvs(a) -> dict:
    out = {}
    for item in a.csv or []:
        if "=" not in item:
            raise SystemExit("--csv takes SYMBOL=path, e.g. --csv XAUUSD=gold.csv")
        sym, path = item.split("=", 1)
        out[fx.resolve(sym)] = path
    return out


def _states(a):
    if a.market == "fx" and getattr(a, "source", "sim") != "sim":
        out = []
        for sym in fx.INSTRUMENTS:
            bars, quote = _fetch_or_exit(sym, a)
            done = live.completed_bars(bars, quote.time)
            out.append(fx.state_at(sym, done, len(done) - 1))
        return out
    if a.market == "fx":
        return fx.snapshot(seed=a.seed, bars=a.bars, csvs=_csvs(a))
    return markets.generate(n=a.n, seed=a.seed)


def _fetch_or_exit(sym, a):
    try:
        return feeds.fetch(sym, a.source, getattr(a, "range", "5y"))
    except feeds.FeedError as e:
        raise SystemExit(f"  {sym}: {e}")


def _data_label(a, n):
    if a.market != "fx":
        return ""
    csvs = _csvs(a)
    src = a.source if getattr(a, "source", "sim") != "sim" else ("csv" if csvs else "sim")
    return f"{src} forex + gold ({n} instruments: {', '.join(fx.INSTRUMENTS)})"


def cmd_decisions(a):
    states = _states(a)
    book = Book()
    records = run(states, book, Limits(**_lim(a)), engine=a.engine)
    print(render.header(a.engine, len(states), _data_label(a, len(states))))
    print(render.decisions_table(records))


def cmd_run(a):
    states = _states(a)
    book = Book()
    records = run(states, book, Limits(**_lim(a)), engine=a.engine)
    print(render.header(a.engine, len(states), _data_label(a, len(states))))
    print(render.decisions_table(records))
    print()
    print(render.book_summary(book))


def cmd_card(a):
    states = _states(a)
    book = Book()
    records = run(states, book, Limits(**_lim(a)), engine=a.engine)
    want = fx.ALIASES.get(a.symbol.upper(), a.symbol.upper()) if a.market == "fx" else a.symbol.upper()
    match = [r for r in records if r[0].symbol.upper() == want]
    if not match:
        raise SystemExit(f"{a.symbol} not in this batch. try: reflex decisions")
    print(render.decision_card(*match[0]))


def cmd_backtest(a):
    settings = backtest.Settings(risk_pct=a.risk, sl_atr=a.sl_atr, tp_atr=a.tp_atr,
                                 max_hold=a.max_hold)
    symbols = [fx.resolve(x) for x in (a.symbols or list(fx.INSTRUMENTS))]
    csvs = _csvs(a)
    lim = Limits(**_lim(a))
    if a.engine == "jev":
        print("  note: --engine jev makes one API call per bar per instrument")
    for i, sym in enumerate(symbols):
        if a.sweep:
            rows = [(seed, backtest.run(sym, fx.simulate(sym, a.bars, seed, not a.trendless), settings, lim,
                                        a.engine, "sim"))
                    for seed in range(1, a.sweep + 1)]
            kind = "random-walk (no trends)" if a.trendless else "simulated"
            print(rule_title(f"SWEEP  ·  {sym}  ·  {a.sweep} {kind} histories "
                             f"of {a.bars} bars"))
            print(render.sweep_table(rows))
        else:
            if sym in csvs:
                bars, src = fx.load_csv(csvs[sym]), csvs[sym]
            elif a.source != "sim":
                bars, quote = _fetch_or_exit(sym, a)
                bars = live.completed_bars(bars, quote.time)
                src = f"{a.source} ({feeds.TICKERS[a.source][sym]}, saved to data/)"
            else:
                bars = fx.simulate(sym, a.bars, a.seed, not a.trendless)
                src = f"sim (seed {a.seed}{', no trends' if a.trendless else ''})"
            res = backtest.run(sym, bars, settings, lim, a.engine, src)
            print(render.backtest_report(res, settings, a.engine))
            if a.trades_csv:
                path = a.trades_csv.replace(".csv", f"_{sym}.csv")
                _write_trades(res, path)
                print(f"  trades written to {path}")
        if i < len(symbols) - 1:
            print()


def cmd_live(a):
    import time
    settings = backtest.Settings(risk_pct=a.risk, sl_atr=a.sl_atr, tp_atr=a.tp_atr,
                                 max_hold=a.max_hold)
    symbols = [fx.resolve(x) for x in (a.symbols or list(fx.INSTRUMENTS))]
    acct = live.Account.load(a.account, a.start)
    acct.config = {"risk_pct": a.risk, "sl_atr": a.sl_atr, "tp_atr": a.tp_atr,
                   "max_hold": a.max_hold, "source": a.source}
    print(render.live_banner(a.source, symbols, a.account, a.watch))
    while True:
        events = live.check(acct, symbols, a.source, settings, Limits(**_lim(a)), a.engine)
        acct.record(events)
        acct.save(a.account)
        if a.dashboard:
            dashboard.write(acct, a.dashboard, a.source)
            print(f"  dashboard updated: {a.dashboard}")
        print(render.live_events(events))
        print(render.account_status(acct))
        if not a.watch:
            break
        print(f"  next check in {a.watch} min · Ctrl+C to stop (the account is saved)")
        try:
            time.sleep(a.watch * 60)
        except KeyboardInterrupt:
            print("\n  stopped. run the same command again to continue.")
            break


def cmd_account(a):
    if a.reset:
        if os.path.exists(a.account):
            os.replace(a.account, a.account + ".bak")
            print(f"  account reset. old one kept as {a.account}.bak")
        else:
            print("  no account yet; it is created on the first `live` run")
        return
    if not os.path.exists(a.account):
        raise SystemExit("  no paper account yet. start one with: python -m jev_bot live")
    acct = live.Account.load(a.account)
    print(render.account_status(acct, history=True))


def cmd_dashboard(a):
    if not os.path.exists(a.account):
        raise SystemExit("  no paper account yet. start one with: python -m jev_bot live")
    acct = live.Account.load(a.account)
    path = dashboard.write(acct, a.out, (acct.config or {}).get("source", "yahoo"))
    print(f"  dashboard written to {path}. open it in a browser.")


def rule_title(t):
    return "\n".join([render.rule("="), "  " + t, render.rule("=")])


def _write_trades(res, path):
    import csv
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "side", "entry_date", "entry", "stop", "target", "units",
                    "exit_date", "exit", "reason", "pips", "pnl", "bars_held"])
        for t in res.trades:
            w.writerow([res.symbol, t.side, t.entry_date, t.entry, round(t.stop, 6),
                        round(t.target, 6), round(t.units, 4), t.exit_date, t.exit,
                        t.reason, t.pips, t.pnl, t.bars])


def _lim(a):
    out = {}
    if a.min_confidence is not None:
        out["min_confidence"] = a.min_confidence
    return out


def build_parser():
    p = argparse.ArgumentParser(prog="jev-bot",
                                description="JEV-powered market decision bot")
    p.add_argument("--engine", default="offline", choices=["offline", "jev"])
    p.add_argument("--n", type=int, default=8)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--min-confidence", type=float, default=None)
    p.add_argument("--source", default="sim", choices=["sim"] + list(feeds.SOURCES))
    p.add_argument("--range", default="5y")
    p.add_argument("--market", default="sim", choices=["sim", "fx"],
                   help="sim = original stocks/crypto/memes; fx = XAUUSD + EURUSD")
    p.add_argument("--bars", type=int, default=750,
                   help="simulated daily bars per instrument (fx)")
    p.add_argument("--csv", action="append", metavar="SYMBOL=PATH",
                   help="real daily history, e.g. --csv XAUUSD=gold.csv (repeatable)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("decisions", help="show the decision table")
    d.set_defaults(func=cmd_decisions)
    r = sub.add_parser("run", help="execute approved decisions on paper")
    r.set_defaults(func=cmd_run)
    c = sub.add_parser("card", help="unpack one decision")
    c.add_argument("symbol")
    c.set_defaults(func=cmd_card)
    bt = sub.add_parser("backtest", help="walk XAUUSD / EURUSD history on paper")
    bt.add_argument("symbols", nargs="*", help="XAUUSD, EURUSD (default: both)")
    bt.add_argument("--risk", type=float, default=0.01, help="fraction of equity per trade")
    bt.add_argument("--sl-atr", type=float, default=1.5)
    bt.add_argument("--tp-atr", type=float, default=3.0)
    bt.add_argument("--max-hold", type=int, default=20)
    bt.add_argument("--sweep", type=int, default=0,
                    help="run N simulated histories and show the spread of results")
    for sp in (d, r, c, bt):          # accept the shared options after the command too
        sp.add_argument("--csv", action="append", metavar="SYMBOL=PATH",
                        default=argparse.SUPPRESS)
        sp.add_argument("--bars", type=int, default=argparse.SUPPRESS)
        sp.add_argument("--seed", type=int, default=argparse.SUPPRESS)
        sp.add_argument("--engine", choices=["offline", "jev"], default=argparse.SUPPRESS)
        sp.add_argument("--market", choices=["sim", "fx"], default=argparse.SUPPRESS)
    lv = sub.add_parser("live", help="paper-trade XAUUSD / EURUSD on REAL live prices")
    lv.add_argument("symbols", nargs="*", help="XAUUSD, EURUSD (default: both)")
    lv.add_argument("--watch", type=int, default=0, metavar="MIN",
                    help="keep running, re-checking every MIN minutes (e.g. 15)")
    lv.add_argument("--account", default="paper_account.json")
    lv.add_argument("--start", type=float, default=10_000.0, help="starting fake balance")
    lv.add_argument("--risk", type=float, default=0.01)
    lv.add_argument("--sl-atr", type=float, default=1.5)
    lv.add_argument("--tp-atr", type=float, default=3.0)
    lv.add_argument("--max-hold", type=int, default=20)
    lv.add_argument("--source", default="yahoo", choices=list(feeds.SOURCES))
    lv.add_argument("--engine", choices=["offline", "jev"], default=argparse.SUPPRESS)
    lv.add_argument("--dashboard", default="", metavar="HTML",
                    help="also rewrite this dashboard page after each check, e.g. docs/index.html")
    lv.set_defaults(func=cmd_live)

    db = sub.add_parser("dashboard", help="build the dashboard page from the paper account")
    db.add_argument("--account", default="paper_account.json")
    db.add_argument("--out", default="docs/index.html")
    db.set_defaults(func=cmd_dashboard)

    ac = sub.add_parser("account", help="show the live paper account")
    ac.add_argument("--account", default="paper_account.json")
    ac.add_argument("--reset", action="store_true", help="start over (old file kept as .bak)")
    ac.set_defaults(func=cmd_account)

    for sp in (d, r, c, bt):
        sp.add_argument("--source", default=argparse.SUPPRESS,
                        choices=["sim"] + list(feeds.SOURCES),
                        help="sim (default), or real data: yahoo, twelvedata")
        sp.add_argument("--range", default=argparse.SUPPRESS,
                        help="history to download for yahoo, e.g. 2y, 5y, 10y")
    bt.add_argument("--trendless", action="store_true",
                    help="simulate pure random walks: the control test, no edge exists")
    bt.add_argument("--trades-csv", default="", help="write every trade to this CSV")
    bt.set_defaults(func=cmd_backtest)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    if getattr(a, "source", "sim") != "sim" and a.cmd in ("decisions", "run", "card"):
        a.market = "fx"
    a.func(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
