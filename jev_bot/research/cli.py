"""`python -m jev_bot research ...`: the research loop from the command line.

    research list                        every idea in the ledger
    research search --market XAUUSD --status failed      (agents run this first)
    research add "idea" --desk idea --market XAUUSD --source "Price Analyst" --falsify "..."
    research predict H4 --return 6 --win-rate 45 --max-dd 8 --min-trades 30 --fail-return-below 0
    research backtest H4 --source yahoo                  development period only
    research sealed H4 --source yahoo                    the one-time holdout run
    research revise H4 "tightened the stop to 1.2 ATR"   counts as another attempt (cap 5)
    research status H4 retired
    research wire H4 daily --strategy "jev_bot/meanrev.py v1"
    research lesson H4 "only works when gold trends"
    research review                                      the weekly critic
    research export-mt5 EURUSD XAUUSD --days 60          1-minute history for scalper tests (Windows + MT5)
"""

from __future__ import annotations

import argparse
import json
import os
from types import SimpleNamespace

from . import honest, ledger, review

PAPERISH = ("paper", "passed_paper", "paused")


def _load():
    return ledger.load()


def _fail(msg: str):
    raise SystemExit("  " + msg)


def cmd_list(a):
    data = _load()
    hs = ledger.search(data, a.market, a.regime, a.status, a.desk, a.text) if a.cmd_r == "search" else data["hypotheses"]
    if a.json:
        print(json.dumps(hs, indent=2, ensure_ascii=False))
        return
    if not hs:
        print("  nothing in the ledger matches")
        return
    for h in hs:
        last = (h.get("lessons") or [{}])[-1].get("text", "")
        print(f"  {h['id']:<4} {ledger.STATUSES.get(h['status'], h['status']):<22} {h.get('desk', ''):<11} "
              f"{','.join(h.get('markets', [])):<15} v{h.get('version', 1)} · {h.get('variations', 0)}/"
              f"{h.get('max_variations', 5)} attempts · {h.get('regime') or '-'}")
        print(f"       {h['idea']}")
        if last:
            print(f"       lesson: {last}")


def cmd_add(a):
    data = _load()
    h = ledger.add(data, a.idea, a.desk, [m.upper() for m in a.market], a.source, a.falsify, a.strategy)
    ledger.save(data)
    print(f"  added {h['id']}. Next: write its prediction BEFORE any test:\n"
          f"  python -m jev_bot research predict {h['id']} --return .. --win-rate .. --max-dd .. --min-trades ..")


def cmd_predict(a):
    data = _load()
    h = ledger.get(data, a.id)
    fail_if = {k: v for k, v in (("return_below", a.fail_return_below), ("drawdown_above", a.fail_dd_above),
                                 ("win_rate_below", a.fail_win_rate_below), ("dsr_below", a.fail_dsr_below))
               if v is not None}
    try:
        p = ledger.predict(h, a.ret, a.win_rate, a.max_dd, a.min_trades, fail_if)
    except ledger.LedgerError as e:
        _fail(str(e))
    ledger.save(data)
    print(f"  prediction for {h['id']} written and locked {p['written']} (sha256 {p['sha256'][:12]}…)")


# --- data ------------------------------------------------------------------------------

def _daily_bars(h, a):
    from .. import feeds, fx, live
    syms = [fx.resolve(m) for m in (h.get("markets") or list(fx.INSTRUMENTS))]
    csvs = dict(c.split("=", 1) for c in a.csv)
    out = {}
    for s in syms:
        if s in csvs:
            out[s] = fx.load_csv(csvs[s])
        elif a.source == "sim":
            out[s] = fx.simulate(s, a.bars, a.seed)
        else:
            try:
                bars, quote = feeds.fetch(s, a.source, a.range)
            except feeds.FeedError as e:
                _fail(f"{s}: {e}")
            out[s] = live.completed_bars(bars, quote)
    label = "sim" if a.source == "sim" and not csvs else ("csv" if csvs else a.source)
    return out, label


def _scalp_series(h, a):
    from ..scalp import backtest as sb, instruments
    syms = [instruments.get(m).symbol for m in (h.get("markets") or ["EURUSD", "XAUUSD"])]
    csvs = {instruments.get(k).symbol: v for k, v in (c.split("=", 1) for c in a.csv)}
    if a.source == "sim":
        return {s: sb.simulate(s, a.days, a.seed) for s in syms}, "sim"
    missing = [s for s in syms if s not in csvs]
    if missing:
        _fail("scalper tests need 1-minute history for " + ", ".join(missing) + ". On the Windows PC "
              "with MT5 run:  python -m jev_bot research export-mt5 " + " ".join(syms) + " --days 60\n"
              "  then:  --csv " + " --csv ".join(f"{s}=data/mt5/{s}_M1.csv" for s in syms))
    return {s: sb.load_csv(csvs[s]) for s in syms}, "csv (MT5 1-minute)"


def _scalp_cfg(desk):
    from ..scalp.strategy import ScalpConfig
    if desk == "scalper_orb":
        from ..scalp.runner import orb_config
        return orb_config(SimpleNamespace(crypto_fee=0.0005, fx_commission=0.0))
    return ScalpConfig()


def _guard(h, need_prediction=True):
    if h.get("desk") == "idea":
        _fail(f"{h['id']} is still an idea: wire it into a strategy (desk daily / scalper_a / scalper_orb) "
              "before it can be backtested")
    if need_prediction and not h.get("prediction") and h.get("status") not in PAPERISH:
        _fail(f"{h['id']} has no written prediction. Write it first, then test:\n"
              f"  python -m jev_bot research predict {h['id']} --return .. --win-rate .. --max-dd .. --min-trades ..")
    if h.get("status") in ("failed", "retired"):
        _fail(f"{h['id']} is {h['status']}. Add a new hypothesis instead of re-testing it.")


def _print_result(h, r, dry):
    print(f"\n  {h['id']} · {h['idea']}")
    print(f"  {r.get('period', '')} · data {r.get('data')} · sealed from {r.get('sealed_from', '-')}")
    print(f"  return {r['return_pct']:+.2f}%  drawdown {r['max_drawdown_pct']:.2f}%  trades {r['trades']}  "
          f"win {r['win_rate_pct']:.0f}%  Sharpe {r['sharpe_ann']:.2f}/yr  DSR {r.get('dsr', 0):.2f}")
    for w in r.get("walk_forward", []):
        print(f"    window {w['first']} → {w['last']}: {w['return_pct']:+.2f}% ({w['trades']} trades)")
    for c in r.get("checks", []):
        tag = "  -  " if c["passed"] is None else ("PASS" if c["passed"] else "FAIL")
        print(f"    [{tag}] {c['name']}: {c['detail']}")
    print(f"  => {'PASSED' if r.get('passed') else 'FAILED'}" + ("   (dry run on simulated data: nothing written)" if dry else ""))


def cmd_backtest(a):
    data = _load()
    h = ledger.get(data, a.id)
    _guard(h)
    trials = h.get("variations", 0) + 1                     # this run counts
    sharpes = [x["sharpe"] for x in h.get("attempts", []) if x.get("sharpe") is not None]
    if h["desk"] == "daily":
        bars, label = _daily_bars(h, a)
        r = honest.daily(h, bars, trials, sharpes, a.windows)
    else:
        series, label = _scalp_series(h, a)
        r = honest.scalper(h, series, _scalp_cfg(h["desk"]), trials, sharpes, a.windows)
    r["data"] = label
    dry = label == "sim"
    _print_result(h, r, dry)
    if dry:
        return
    ledger.record_attempt(h, r)
    failed = [c["name"].lower() for c in r["checks"] if not c["passed"]]
    if h["status"] in PAPERISH:
        ledger.add_lesson(h, f"honest backtest v{h['version']} {'passed' if r['passed'] else 'failed'}"
                          + (f": {', '.join(failed)}" if failed else ""), "backtest")
    elif r["passed"]:
        h["status"] = "passed_backtest"
        ledger.add_lesson(h, f"v{h['version']} passed the development backtest; the sealed test is next", "backtest")
    else:
        h["status"] = "testing"
        ledger.add_lesson(h, f"v{h['version']} failed: {', '.join(failed)}", "backtest")
        if h["variations"] >= h.get("max_variations", 5):
            h["status"] = "failed"
            ledger.add_lesson(h, f"hit the rewrite cap ({h['variations']} versions) without passing", "ledger")
    ledger.save(data)
    print(f"  saved to the ledger: attempt {h['variations']} of {h.get('max_variations', 5)}, status {h['status']}")


def cmd_sealed(a):
    data = _load()
    h = ledger.get(data, a.id)
    _guard(h)
    if h.get("sealed"):
        _fail(f"{h['id']} already used its sealed test ({h['sealed']['at'][:10]}). It gets exactly one.")
    if h["status"] not in ("passed_backtest",) + PAPERISH:
        _fail(f"{h['id']} has not passed the development backtest (status {h['status']}). "
              "The sealed period is only for a strategy that already survived everything else.")
    if h["status"] in PAPERISH and not h.get("backtest"):
        _fail(f"run the development backtest first: python -m jev_bot research backtest {h['id']}")
    if h["desk"] == "daily":
        bars, label = _daily_bars(h, a)
        r = honest.daily_sealed(h, bars)
    else:
        series, label = _scalp_series(h, a)
        r = honest.scalper_sealed(h, series, _scalp_cfg(h["desk"]))
    r["data"] = label
    print(f"\n  SEALED TEST · {h['id']} · {r['period']}")
    print(f"  return {r['return_pct']:+.2f}%  drawdown {r['max_drawdown_pct']:.2f}%  trades {r['trades']}  "
          f"win {r['win_rate_pct']:.0f}%  => {'PASSED' if r['passed'] else 'FAILED'} {'; '.join(r['why'])}")
    if label == "sim":
        print("  (dry run on simulated data: the sealed test is NOT used up)")
        return
    ledger.record_sealed(h, r)
    if h["status"] == "passed_backtest":
        h["status"] = "passed_sealed" if r["passed"] else "failed"
    ledger.add_lesson(h, f"sealed test {'passed' if r['passed'] else 'failed'} ({r['return_pct']:+.2f}% on "
                         f"{r['period']})" + (": " + "; ".join(r["why"]) if r["why"] else ""), "sealed")
    ledger.save(data)


def cmd_revise(a):
    data = _load()
    h = ledger.get(data, a.id)
    try:
        ledger.revise(h, a.note)
    except ledger.LedgerError as e:
        ledger.save(data)
        _fail(str(e))
    ledger.save(data)
    print(f"  {h['id']} is now v{h['version']}: attempt {h['variations'] + 1} of {h['max_variations']} next")


def cmd_status(a):
    data = _load()
    h = ledger.get(data, a.id)
    try:
        ledger.set_status(h, a.status)
    except ledger.LedgerError as e:
        _fail(str(e))
    if a.note:
        ledger.add_lesson(h, a.note)
    ledger.save(data)
    print(f"  {h['id']} → {ledger.STATUSES[a.status]}")


def cmd_wire(a):
    data = _load()
    h = ledger.get(data, a.id)
    h["desk"], h["strategy"] = a.desk, a.strategy
    if h["status"] == "untested":
        h["status"] = "testing"
    ledger.save(data)
    print(f"  {h['id']} now runs on the {a.desk} desk ({a.strategy})")


def cmd_lesson(a):
    data = _load()
    h = ledger.get(data, a.id)
    ledger.add_lesson(h, a.text, a.source)
    ledger.save(data)
    print(f"  noted on {h['id']}")


def cmd_review(a):
    r = review.run(a.account, {"a": a.scalp_account, "orb": a.orb_account})
    print(review.markdown(r))


def cmd_export_mt5(a):
    """1-minute candles from the logged-in MT5 terminal, for scalper research backtests."""
    import time
    from ..scalp import feeds as sf, instruments
    try:
        import MetaTrader5 as mt5                     # Windows only
    except ImportError:
        _fail("needs the MetaTrader5 package (Windows, with MT5 running): pip install MetaTrader5")
    if not mt5.initialize():
        _fail(f"could not connect to MT5: {mt5.last_error()}")
    syms = [instruments.get(s).symbol for s in a.symbols]
    names = sf.resolve_mt5_symbols(syms, [s.name for s in mt5.symbols_get()], sf.mt5_overrides())
    os.makedirs(a.out, exist_ok=True)
    end = int(time.time())
    for s in syms:
        theirs = names.get(s)
        if not theirs:
            print(f"  {s}: not offered by this broker, skipped")
            continue
        mt5.symbol_select(theirs, True)
        rates = mt5.copy_rates_range(theirs, mt5.TIMEFRAME_M1, end - a.days * 86400, end)
        if rates is None or not len(rates):
            print(f"  {s}: no history returned ({mt5.last_error()})")
            continue
        path = os.path.join(a.out, f"{s}_M1.csv")
        with open(path, "w") as fh:
            fh.write("time,open,high,low,close\n")
            for r in rates:
                fh.write(f"{int(r['time'])},{r['open']},{r['high']},{r['low']},{r['close']}\n")
        print(f"  {s}: {len(rates)} candles → {path}")
    mt5.shutdown()


def add_parser(sub) -> None:
    rp = sub.add_parser("research", help="the research loop: ledger, predictions, honest backtests, weekly review")
    rs = rp.add_subparsers(dest="cmd_r", required=True)

    for name in ("list", "search"):
        x = rs.add_parser(name, help="show the ledger" if name == "list" else "find past ideas before proposing one")
        x.add_argument("--market", default="")
        x.add_argument("--regime", default="")
        x.add_argument("--status", default="")
        x.add_argument("--desk", default="")
        x.add_argument("--text", default="")
        x.add_argument("--json", action="store_true")
        x.set_defaults(func=cmd_list)

    x = rs.add_parser("add", help="add an idea to the ledger")
    x.add_argument("idea")
    x.add_argument("--desk", default="idea", choices=list(ledger.DESKS))
    x.add_argument("--market", action="append", default=[])
    x.add_argument("--source", default="you", help="who proposed it, e.g. 'Price Analyst'")
    x.add_argument("--falsify", default="", help="what result would prove it wrong")
    x.add_argument("--strategy", default="", help="file and version that runs it")
    x.set_defaults(func=cmd_add)

    x = rs.add_parser("predict", help="write the prediction BEFORE testing (locked forever)")
    x.add_argument("id")
    x.add_argument("--return", dest="ret", type=float, required=True, help="expected return, %%")
    x.add_argument("--win-rate", type=float, required=True, help="expected win rate, %%")
    x.add_argument("--max-dd", type=float, required=True, help="worst drawdown you expect, %%")
    x.add_argument("--min-trades", type=int, required=True)
    x.add_argument("--fail-return-below", type=float, default=None)
    x.add_argument("--fail-dd-above", type=float, default=None)
    x.add_argument("--fail-win-rate-below", type=float, default=None)
    x.add_argument("--fail-dsr-below", type=float, default=None, help="default 0.95")
    x.set_defaults(func=cmd_predict)

    for name, fn, hlp in (("backtest", cmd_backtest, "development-period test: walk-forward, stress, DSR"),
                          ("sealed", cmd_sealed, "the ONE run on the sealed newest 25%% of history")):
        x = rs.add_parser(name, help=hlp)
        x.add_argument("id")
        x.add_argument("--source", default="yahoo", choices=["yahoo", "twelvedata", "sim", "csv"])
        x.add_argument("--range", default="10y")
        x.add_argument("--csv", action="append", default=[], metavar="SYMBOL=PATH")
        x.add_argument("--bars", type=int, default=2500, help="simulated daily bars (sim)")
        x.add_argument("--days", type=int, default=40, help="simulated days of 1-minute bars (scalper sim)")
        x.add_argument("--seed", type=int, default=7)
        x.add_argument("--windows", type=int, default=4)
        x.set_defaults(func=fn)

    x = rs.add_parser("revise", help="record a rewrite after a failed backtest (capped)")
    x.add_argument("id")
    x.add_argument("note")
    x.set_defaults(func=cmd_revise)

    x = rs.add_parser("status", help="set a hypothesis' status, e.g. paused or retired")
    x.add_argument("id")
    x.add_argument("status", choices=list(ledger.STATUSES))
    x.add_argument("--note", default="")
    x.set_defaults(func=cmd_status)

    x = rs.add_parser("wire", help="attach an idea to the strategy code that runs it")
    x.add_argument("id")
    x.add_argument("desk", choices=[d for d in ledger.DESKS if d != "idea"])
    x.add_argument("--strategy", required=True, help="file and version, e.g. jev_bot/scalp/meanrev.py v1")
    x.set_defaults(func=cmd_wire)

    x = rs.add_parser("lesson", help="write a lesson into the ledger")
    x.add_argument("id")
    x.add_argument("text")
    x.add_argument("--source", default="you")
    x.set_defaults(func=cmd_lesson)

    x = rs.add_parser("review", help="the weekly critic: paper vs backtest vs prediction")
    x.add_argument("--account", default="state/paper_account.json")
    x.add_argument("--scalp-account", default="state/scalper_account.json")
    x.add_argument("--orb-account", default="state/scalper_orb.json")
    x.set_defaults(func=cmd_review)

    x = rs.add_parser("export-mt5", help="save 1-minute history from MT5 for scalper tests (Windows)")
    x.add_argument("symbols", nargs="+")
    x.add_argument("--days", type=int, default=60)
    x.add_argument("--out", default="data/mt5")
    x.set_defaults(func=cmd_export_mt5)
