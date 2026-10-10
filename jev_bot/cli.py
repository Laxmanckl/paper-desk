"""reflex command line.

    reflex decisions        one cycle: state -> JEV -> risk, show the table
    reflex run              ... and execute the approved ones on paper
    reflex card SYMBOL      unpack a single decision
    reflex backtest         walk XAUUSD / EURUSD history on paper (forex + gold)
    reflex live             paper-trade XAUUSD / EURUSD on real live prices
    reflex account          show the live paper account
    reflex dashboard        build the dashboard web page from the account
    reflex scalp            run the scalper live (crypto via Binance, forex/gold via MT5)
    reflex scalp-backtest   test the scalping rules on 1-minute history
    reflex research ...     the research loop: ledger, predictions, honest backtests, weekly critic

Add --market fx to decisions / run / card for gold and EUR/USD.

Offline decision engine by default (no key, reproducible). Pass
--engine jev with TYPESAFE_API_KEY set to use the real TypeSafe model.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import render, markets, fx, backtest, feeds, live, dashboard, alerts
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
            done = live.completed_bars(bars, quote)
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
                bars = live.completed_bars(bars, quote)
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
    import traceback
    settings = backtest.Settings(risk_pct=a.risk, sl_atr=a.sl_atr, tp_atr=a.tp_atr,
                                 max_hold=a.max_hold)
    symbols = [fx.resolve(x) for x in (a.symbols or list(fx.INSTRUMENTS))]
    acct = live.Account.load(a.account, a.start)
    acct.config = {"risk_pct": a.risk, "sl_atr": a.sl_atr, "tp_atr": a.tp_atr,
                   "max_hold": a.max_hold, "source": a.source}
    acct.runner = {"kind": a.runner, "every_min": a.watch or None}
    print(render.live_banner(a.source, symbols, a.account, a.watch))
    last_publish = 0.0
    started_code = _code_stamp()
    while True:
        started = time.time()
        before_closed, before_open = len(acct.closed), set(acct.positions)
        try:
            events = live.check(acct, symbols, a.source, settings, Limits(**_lim(a)), a.engine)
        except Exception as e:             # never let one bad check stop a 24/5 runner
            traceback.print_exc()
            events = [f"BOT: check failed ({type(e).__name__}: {e}); retrying next check"]
        acct.record(events)
        if alerts.configured():
            try:
                alerts.notify(acct, events, before_closed, before_open)
            except Exception:
                traceback.print_exc()
        acct.save(a.account)
        if a.dashboard:
            dashboard.write(acct, a.dashboard, a.source)
        if events or not a.watch:
            print(render.live_events(events))
        if not a.watch:
            print(render.account_status(acct))
            break
        if a.publish_every and time.time() - last_publish >= a.publish_every * 60:
            try:
                ok = publish(a.account)
            except Exception:
                traceback.print_exc()
                ok = False
            last_publish = time.time() if ok else last_publish + 60    # retry a minute later
            if ok and a.runner == "server" and _code_stamp() != started_code:
                # the pull brought new code (e.g. kill switch rules): exit so systemd
                # (Restart=always) starts the bot again on the new version
                acct.save(a.account)
                print("  code updated from GitHub; restarting to use it", flush=True)
                os._exit(0)
        try:
            time.sleep(max(1.0, a.watch * 60 - (time.time() - started)))
        except KeyboardInterrupt:
            print("\n  stopped. run the same command again to continue.")
            break


def _code_stamp() -> float:
    """Newest modification time of the bot's code: changes when a git pull brings an update."""
    from pathlib import Path
    root = Path(__file__).resolve().parent
    return max((f.stat().st_mtime for f in root.rglob("*.py")), default=0.0)


def publish(*account_paths: str) -> bool:
    """Commit the account file and push it, so GitHub rebuilds the dashboard.
    Used by the always-on server. Failures are reported and retried later."""
    import subprocess
    from datetime import datetime, timezone

    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")       # never wait for a password prompt

    def git(*args):
        try:
            return subprocess.run(["git", *args], capture_output=True, text=True, timeout=120, env=env)
        except (subprocess.TimeoutExpired, OSError) as e:      # a stuck or missing git must not stop the bot
            return subprocess.CompletedProcess(args, 1, "", f"{type(e).__name__}: {e}")

    git("add", *[p for p in account_paths if os.path.exists(p)])
    if git("diff", "--cached", "--quiet").returncode != 0:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        git("commit", "-q", "-m", f"server check {stamp}")
    # The server is the only trader in server mode, so if the account file was
    # also changed on GitHub (a run during the hand-over), keep the server's copy.
    pull = git("pull", "-q", "--rebase", "-X", "theirs", "--autostash", "origin", "main")
    if pull.returncode != 0:
        git("rebase", "--abort")
    push = git("push", "-q", "origin", "HEAD:main")
    if push.returncode != 0:
        print(f"  publish failed, will retry: {(push.stderr or pull.stderr).strip()[:200]}")
        return False
    return True


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


def cmd_scalp(a):
    from .scalp import runner
    runner.run(a)


def cmd_scalp_dashboard(a):
    from .scalp import runner
    from .scalp.book import ScalpBook
    if not os.path.exists(a.account):
        raise SystemExit(f"  no scalper account at {a.account}")
    note = "Snapshot, updated about every 15 minutes. The live page on the bot's computer updates every second."
    books = {"a": ScalpBook.load(a.account)}
    if a.orb_account and os.path.exists(a.orb_account):
        books["orb"] = ScalpBook.load(a.orb_account)
    peers = [runner.desk_summary(k, b) for k, b in books.items()]
    outs = {"a": a.out, "orb": os.path.join(os.path.dirname(a.out) or ".", runner.DESKS["orb"]["page"])}
    for k, b in books.items():
        runner.build_static(b, outs[k], note, k, peers)
        print(f"  scalper dashboard ({runner.DESKS[k]['name']}) written to {outs[k]}")


def cmd_scalp_backtest(a):
    from .scalp import backtest as sb
    sb.cli(a)


def cmd_alert_test(a):
    if not alerts.configured():
        raise SystemExit("  set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first (server/telegram.sh does this)")
    ok = alerts.send("👋 Paper desk alerts are working. You'll get a message when a trade opens "
                     "or closes, a daily summary after the market closes, and a warning if the bot has trouble.")
    print("  sent" if ok else "  could not send; check the bot token and chat id")
    if not ok:
        raise SystemExit(1)


def cmd_heartbeat(a):
    """Exit with an error if the account has not been checked recently.
    GitHub runs this hourly in server mode, so a dead server shows as a failed
    run and GitHub emails the owner."""
    import json
    from datetime import datetime, timezone
    with open(a.account) as fh:
        raw = json.load(fh)
    if "last_tick" in raw:                                  # the scalper's account
        if not raw["last_tick"]:
            raise SystemExit("  scalper has not received a price yet")
        age = (datetime.now(timezone.utc).timestamp() - raw["last_tick"]) / 60
        runner = "scalper"
    else:
        acct = live.Account.load(a.account)
        stamp = acct.last_check or (acct.equity_log[-1][0] if acct.equity_log else "")
        if not stamp:
            raise SystemExit("  no checks recorded yet")
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() / 60
        runner = (acct.runner or {}).get("kind", "?")
    if age > a.max_age:
        svc = "paper-scalper" if runner == "scalper" else "paper-desk"
        raise SystemExit(f"  BOT IS NOT RUNNING: last check {age:.0f} min ago (runner: {runner}). "
                         f"Log in to the server and run: sudo systemctl status {svc}")
    print(f"  ok: last check {age:.0f} min ago (runner: {runner})")


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
    lv.add_argument("--runner", default="computer", choices=["computer", "github", "server"],
                    help="shown on the dashboard: where the bot is running")
    lv.add_argument("--publish-every", type=int, default=0, metavar="MIN",
                    help="server mode: git commit + push the account every MIN minutes")
    lv.add_argument("--dashboard", default="", metavar="HTML",
                    help="also rewrite this dashboard page after each check, e.g. docs/index.html")
    lv.set_defaults(func=cmd_live)

    sc = sub.add_parser("scalp", help="run the scalper live (1-min signals, exits every second)")
    sc.add_argument("--crypto", default="majors", help="majors, none, or e.g. BTCUSDT,ETHUSDT")
    sc.add_argument("--fx", default="none", help="majors, none, or e.g. EURUSD,XAUUSD (needs MT5 on Windows)")
    sc.add_argument("--account", default="state/scalper_account.json")
    sc.add_argument("--start", type=float, default=10_000.0)
    sc.add_argument("--port", type=int, default=8080, help="live dashboard port (0 = off)")
    sc.add_argument("--publish-every", type=int, default=0, metavar="MIN",
                    help="git commit + push the scalper account every MIN minutes")
    sc.add_argument("--crypto-fee", type=float, default=0.0005, help="per side, e.g. 0.001 for Binance spot")
    sc.add_argument("--fx-commission", type=float, default=0.0, help="USD per 100k units per side")
    sc.add_argument("--orb-account", default="state/scalper_orb.json",
                    help="account for strategy B, the opening-range breakout ('none' = off)")
    sc.add_argument("--restart-on-update", action="store_true",
                    help="exit after a publish pulls new code (a restart loop must start it again)")
    sc.set_defaults(func=cmd_scalp)

    sd = sub.add_parser("scalp-dashboard", help="build the scalper snapshot page for GitHub Pages")
    sd.add_argument("--account", default="state/scalper_account.json")
    sd.add_argument("--orb-account", default="state/scalper_orb.json")
    sd.add_argument("--out", default="docs/scalper.html")
    sd.set_defaults(func=cmd_scalp_dashboard)

    sb_ = sub.add_parser("scalp-backtest", help="test the scalping rules on 1-minute history")
    sb_.add_argument("symbols", nargs="*", help="default: BTCUSDT EURUSD XAUUSD (simulated)")
    sb_.add_argument("--csv", action="append", metavar="SYMBOL=PATH", default=[],
                     help="1-minute candles: time,open,high,low,close (+ optional spread column)")
    sb_.add_argument("--days", type=int, default=20)
    sb_.add_argument("--sweep", type=int, default=0)
    sb_.add_argument("--trendless", action="store_true")
    sb_.add_argument("--strategy", choices=["pullback", "orb"], default="pullback",
                     help="pullback = A (default), orb = B, the opening-range breakout")
    sb_.add_argument("--crypto-fee", type=float, default=0.0005)
    sb_.add_argument("--fx-commission", type=float, default=0.0)
    sb_.set_defaults(func=cmd_scalp_backtest)

    at = sub.add_parser("alert-test", help="send a test Telegram message")
    at.set_defaults(func=cmd_alert_test)

    hb = sub.add_parser("heartbeat", help="fail if the bot has not checked in recently")
    hb.add_argument("--account", default="paper_account.json")
    hb.add_argument("--max-age", type=int, default=45, metavar="MIN")
    hb.set_defaults(func=cmd_heartbeat)

    from .research import cli as research_cli
    research_cli.add_parser(sub)

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
