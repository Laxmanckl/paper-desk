"""Command line.

  python -m jevsys run        decision loop + live page + bridge ingest + GitHub publishing
  python -m jevsys run --sim  same, fed by a built-in price simulator (no MT5 needed)
  python -m jevsys dashboard  build docs/index.html from state/jev_desk.json (GitHub Actions)
  python -m jevsys heartbeat  fail if the server hasn't published recently (GitHub Actions)
  python -m jevsys jev-test   the guide's "fire your first decision": one live Jev call, answers printed
  python -m jevsys drill      test every rung of the fallback ladder on purpose (simulated, never touches the live account)
  python -m jevsys report     the Step 8 report: baselines, calibration, fees
  python -m jevsys alert-test send one Telegram test message
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from dataclasses import replace

from . import dashboard


def _sim_pump(market, stop: threading.Event) -> None:
    from .sim_fx import SimBridge
    now = time.time()
    sim = SimBridge(seed=int(now), start_ts=now - 1_600 * 300)
    while not stop.is_set():
        now = time.time()
        market.ingest(sim.step_to(now), now)
        stop.wait(10)


def cmd_run(a) -> None:
    from .config import StateConfig
    from .desk import Desk
    from .market import MarketStore
    from .publish import Publisher
    from .web import serve

    cfg = replace(StateConfig(), decide_every_min=a.every)
    market = MarketStore(cfg.keep_bars)
    desk = Desk(market, state_path=a.state, runner=a.runner + (" · simulated prices" if a.sim else ""),
                log_dir=a.logs, cfg=cfg)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    if a.port:
        serve(desk, a.port, token="sim" if a.sim else None)
        print(f"live dashboard on http://0.0.0.0:{a.port}", flush=True)
    if a.sim:
        threading.Thread(target=_sim_pump, args=(market, stop), daemon=True, name="sim").start()
        desk.event("Using simulated prices (--sim). Not real market data.")
    if a.publish_every:
        Publisher(desk, a.publish_every, stop).start()
    loop = threading.Thread(target=desk.run, args=(stop,), daemon=True, name="loop")
    loop.start()
    last_print = 0.0
    while not stop.wait(5):
        if not loop.is_alive():
            desk.event("Decision loop stopped unexpectedly; the service will restart it.")
            desk.save()
            sys.exit(1)
        if time.time() - last_print > 900:
            st = desk.state()
            print(f"bridge={st['bridge']['status']} rounds={st['totals']['rounds_session']} "
                  f"tokens_max={st['engine']['tokens_max']}", flush=True)
            last_print = time.time()
    desk.event("Desk stopped.")
    desk.save()
    loop.join(timeout=5)


def cmd_dashboard(a) -> None:
    print("dashboard written to", dashboard.write(a.state, a.out))


def cmd_heartbeat(a) -> None:
    try:
        with open(a.state, encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        sys.exit(f"no desk state at {a.state} yet; run the server setup")
    age_min = (time.time() - (st.get("server_time") or 0)) / 60
    if age_min > a.max_age:
        sys.exit(f"Jev Desk server silent for {age_min:.0f} min (limit {a.max_age}). "
                 "On the server: sudo systemctl status jev-desk ; journalctl -u jev-desk -n 50")
    print(f"server alive: last publish {age_min:.0f} min ago; bridge {st.get('bridge', {}).get('status')}")


GUIDE_SAMPLE = {"mid": 3.4127, "spread_bps": 3.5, "imbalance": 0.71, "realized_vol_5m": 0.034,
                "inventory": -120, "aggressive_buy_ratio": 0.63}  # the guide's Step 2 sample state


def cmd_jev_test(a) -> None:
    """Guide Step 2.4: fire one decision and print the answers. Uses a live snapshot from the
    running desk when there is one (gold first), otherwise the guide's own sample state."""
    import urllib.request
    from .jev import JevLayer
    state, label = GUIDE_SAMPLE, "the guide's sample state"
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{a.port}/api/state", timeout=3) as r:
            ins = json.load(r).get("instruments", {})
        for sym in ["XAUUSD", "EURUSD", *ins]:
            snap = ((ins.get(sym) or {}).get("last") or {}).get("snap")
            if snap:
                state, label = snap, f"the latest live {sym} snapshot"
                break
    except OSError:
        pass
    j = JevLayer()
    if not j.enabled:
        sys.exit(f"  Jev is off: {j.reason}")
    print(f"  Asking Jev ({j.model}) the 6-question battery about {label}...")
    r = j.ask_all({"test": state})["test"]
    if "error" in r:
        hint = {"auth": "the key was rejected; check it on console.typesafe.ai and run server/jev-key.sh again",
                "rate_limit": "rate limited; wait a minute and try again",
                "network": "can't reach api.typesafe.ai from this server",
                "timeout": "TypeSafe didn't answer in time; try again"}.get(r["kind"], r["error"])
        sys.exit(f"  Jev call failed ({r['kind']}): {hint}")
    print(f"  Model: {r['model']}" + ("  (pinned)" if r["pinned_ok"] else f"  WARNING: not the pinned {j.model}"))
    print(f"  Answered in {r['ms']} ms, {r['tokens_in']} input tokens")
    for k, v in r["answers"].items():
        if "value" in v:
            print(f"    {k:18} {v['value']:<14} confidence {v['p']:.0%}")
        elif "label" in v:
            print(f"    {k:18} {v['label']:<14} (score {v['score']:.2f}, confidence {v['p']:.0%})")
        else:
            print(f"    {k:18} {'yes' if v['p'] >= 0.5 else 'no':<14} probability {v['p']:.0%}")
    print("  If those printed, you are talking to Jev.")


def cmd_drill(a) -> None:
    from .drill import run_drill
    print("  Fallback ladder drill (simulated market, separate from the live account):")
    res = run_drill(verbose=True, send=a.send)
    bad = [n for n, ok, _ in res if not ok]
    print(f"\n  {len(res) - len(bad)}/{len(res)} passed." + (" FAILED: " + ", ".join(bad) if bad else " Every rung behaves as the guide says."))
    if bad:
        sys.exit(1)


def cmd_report(a) -> None:
    """Prints the Step 8 report the running desk keeps (refreshed hourly) from its local API."""
    import urllib.request
    from . import report as step8
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{a.port}/api/state?full=1", timeout=10) as r:
            rep = json.load(r).get("report")
    except OSError:
        rep = None
    if not rep:
        try:
            rep = json.load(open(a.state, encoding="utf-8")).get("report")
        except (OSError, ValueError):
            rep = None
    if not rep:
        sys.exit("  No report yet: the desk builds the first one about a minute after it starts, then hourly.")
    print(step8.to_text(rep))


def cmd_alert_test(a) -> None:
    from .alerts import Alerter
    al = Alerter()
    if not al.enabled:
        sys.exit("  Alerts are off: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set. Run: bash ~/paper-desk/server/telegram.sh")
    al.send_now("✅ Jev Desk alerts are on. You'll get kill-switch, Jev-down, bridge-down, trade and daily-summary messages here.")
    print("  Test message sent. Check Telegram.")


def cmd_restart_test(a) -> None:
    """Start the two-week paper test over: empty paper accounts, day 0. Logs are kept.
    Run with the service stopped (server/restart-test.sh does that)."""
    import os
    sdir = os.path.dirname(a.state) or "."
    for name in ("paper_book.json", "shadow_rules.json", "shadow_nogate.json"):
        try:
            os.remove(os.path.join(sdir, name))
        except FileNotFoundError:
            pass
    try:
        st = json.load(open(a.state, encoding="utf-8"))
    except (OSError, ValueError):
        st = None
    if st:
        st["created"] = time.time()
        st["report"] = None
        st.setdefault("log", []).append([round(time.time(), 1), "Two-week paper test restarted: accounts reset to $10,000, day 0."])
        with open(a.state, "w", encoding="utf-8") as fh:
            json.dump(st, fh, separators=(",", ":"))
    print("  Paper accounts reset to $10,000 and the two-week clock restarted. Logs kept.")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="jevsys")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--state", default="state/jev_desk.json")
    r.add_argument("--logs", default="logs")
    r.add_argument("--runner", default="local")
    r.add_argument("--port", type=int, default=8080)
    r.add_argument("--every", type=int, default=15, help="minutes between decisions (5, 10, 15...)")
    r.add_argument("--publish-every", type=float, default=0, help="minutes between GitHub pushes (0 = off)")
    r.add_argument("--sim", action="store_true", help="use simulated prices instead of the MT5 bridge")
    r.set_defaults(func=cmd_run)
    d = sub.add_parser("dashboard")
    d.add_argument("--state", default="state/jev_desk.json")
    d.add_argument("--out", default="docs/index.html")
    d.set_defaults(func=cmd_dashboard)
    h = sub.add_parser("heartbeat")
    h.add_argument("--state", default="state/jev_desk.json")
    h.add_argument("--max-age", type=float, default=45)
    h.set_defaults(func=cmd_heartbeat)
    jt = sub.add_parser("jev-test", help="one live Jev call with the 6-question battery")
    jt.add_argument("--port", type=int, default=8080)
    jt.set_defaults(func=cmd_jev_test)
    dr = sub.add_parser("drill", help="test every rung of the fallback ladder on purpose")
    dr.add_argument("--send", action="store_true", help="also send one real Telegram test message")
    dr.set_defaults(func=cmd_drill)
    rp = sub.add_parser("report", help="the Step 8 report")
    rp.add_argument("--port", type=int, default=8080)
    rp.add_argument("--state", default="state/jev_desk.json")
    rp.set_defaults(func=cmd_report)
    rt = sub.add_parser("restart-test", help="reset the paper accounts and restart the two-week clock")
    rt.add_argument("--state", default="state/jev_desk.json")
    rt.set_defaults(func=cmd_restart_test)
    at = sub.add_parser("alert-test", help="send one Telegram test message")
    at.set_defaults(func=cmd_alert_test)
    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
