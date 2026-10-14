"""Command line.

  python -m jevsys run        the 24/7 loop, live page, GitHub publishing
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

from . import dashboard


def cmd_run(a) -> None:
    from .desk import Desk
    from .feeds.sim import SimFeed
    from .publish import Publisher
    from .web import serve

    feed = SimFeed(n_blocks=None, seed=int(time.time()), start_ts=time.time() + 1)
    desk = Desk(feed, state_path=a.state, runner=a.runner, log_dir=a.logs)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    if a.port:
        serve(desk, a.port)
        print(f"live dashboard on http://0.0.0.0:{a.port}", flush=True)
    if a.publish_every:
        Publisher(desk, a.publish_every, stop).start()
    loop = threading.Thread(target=desk.run, args=(stop,), daemon=True, name="loop")
    loop.start()
    last_print = 0.0
    while not stop.wait(5):
        if not loop.is_alive():
            desk.event("Block loop stopped unexpectedly; service will restart.")
            desk.save()
            sys.exit(1)
        if time.time() - last_print > 300:
            st = desk.state()
            print(f"blocks={st['totals']['blocks_session']} tokens={st['engine']['tokens']} "
                  f"engine_p50={st['engine']['p50_ms']}ms lag={st['engine']['lag_p50_ms']}ms", flush=True)
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
    print(f"server alive: last publish {age_min:.0f} min ago, {st['totals']['blocks']:,} blocks seen")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="jevsys")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--state", default="state/jev_desk.json")
    r.add_argument("--logs", default="logs")
    r.add_argument("--runner", default="local")
    r.add_argument("--port", type=int, default=8080)
    r.add_argument("--publish-every", type=float, default=0, help="minutes between GitHub pushes (0 = off)")
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
