"""Command line.

  python -m jevsys run        decision loop + live page + bridge ingest + GitHub publishing
  python -m jevsys run --sim  same, fed by a built-in price simulator (no MT5 needed)
  python -m jevsys dashboard  build docs/index.html from state/jev_desk.json (GitHub Actions)
  python -m jevsys heartbeat  fail if the server hasn't published recently (GitHub Actions)
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
    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
