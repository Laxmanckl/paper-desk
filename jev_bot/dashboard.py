"""The dashboard: one self-contained HTML page built from the paper account.

    python -m jev_bot dashboard            -> docs/index.html

The page has the account embedded as JSON and draws everything in the
browser (no server, no build step), so it can be hosted anywhere static:
GitHub Pages, Netlify, or opened straight from disk. With --live, the live
command rewrites it after every check.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from . import feeds, fx
from .research import panel

TEMPLATE = Path(__file__).with_name("dashboard_template.html")

_SOURCE_LABEL = {
    "yahoo": "Yahoo Finance (gold: COMEX futures GC=F, EUR/USD: EURUSD=X)",
    "twelvedata": "Twelve Data (spot XAU/USD, EUR/USD)",
    "sim": "simulated",
}


def build(acct, source: str = "yahoo", preview: bool = False,
          auto_refresh_min: int = 5, full_document: bool = True, note: str = "") -> str:
    acct._set_benchmark({sym: lp["price"] for sym, lp in acct.last_price.items() if lp.get("price")})
    data = {
        "account": asdict(acct),
        "instruments": {s: {k: v for k, v in spec.items()} for s, spec in fx.INSTRUMENTS.items()},
        "config": acct.config or {},
        "source_label": _SOURCE_LABEL.get(source, source),
        "preview": preview,
        "auto_refresh_min": auto_refresh_min,
        "note": note,
        "research": panel.safe(panel.daily_panel, acct),
        "has_scalper": os.path.exists(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                   "state", "scalper_account.json")),
    }
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    body = panel.inject(TEMPLATE.read_text(encoding="utf-8")).replace("/*__DATA__*/", blob)
    if not full_document:
        return body
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">"
            "<style>*,*::before,*::after{box-sizing:border-box}body{margin:0}"
            "[hidden]{display:none!important}img{max-width:100%}</style>"
            "</head><body>" + body + "</body></html>")


def write(acct, path: str, source: str = "yahoo", **kw) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(build(acct, source, **kw))
    os.replace(tmp, path)
    return path
