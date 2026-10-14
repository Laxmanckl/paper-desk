"""Builds the dashboard page from the desk state.

Static (GitHub Pages): state embedded, page reloads every 5 min.
Live (server): same page, polls /api/state every second.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

TEMPLATE = Path(__file__).with_name("dashboard_template.html")


def build(state: dict, live: bool = False) -> str:
    blob = json.dumps({"live": live, "state": state}, separators=(",", ":")).replace("</", "<\\/")
    body = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/", blob)
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">"
            "<style>*,*::before,*::after{box-sizing:border-box}body{margin:0}"
            "[hidden]{display:none!important}</style></head><body>" + body + "</body></html>")


def write(state_path: str, out: str) -> str:
    with open(state_path, encoding="utf-8") as fh:
        state = json.load(fh)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(build(state, live=False))
    os.replace(tmp, out)
    return out
