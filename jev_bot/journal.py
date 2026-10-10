"""The decision journal: every trading decision, with its reasoning, kept for good.

    journal/<desk>/<YYYY-MM>.jsonl      one JSON object per line

Each entry:
    t           ISO time (UTC)
    desk        daily, scalper_a, scalper_orb, daily:H4 ...
    strategy    the ledger hypothesis and version that made the call, e.g. "H1 v1"
    symbol
    decision    OPEN, REVERSE, SKIP, BLOCKED, HOLDING, CLOSE
    side        BUY / SELL (when there is one)
    price
    conditions  the market at that moment (regime, momentum, trend, RSI, spread...)
    reasoning   why, in plain words
    result      for CLOSE: pnl, exit reason, R

The bots write it the moment they decide; the servers publish it with the
account; the weekly critic reads it. Writing can never stop a bot: any error
is swallowed.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "journal"


def _dir(desk: str, root=None) -> Path:
    return Path(root or ROOT) / desk.replace(":", "-")


def folder(desk: str, root=None) -> str:
    return str(_dir(desk, root))


def _ts(t) -> datetime:
    if isinstance(t, datetime):
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(t, timezone.utc)
    if isinstance(t, str) and t:
        d = datetime.fromisoformat(t.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


_label_cache: dict = {}


def strategy_label(desk: str) -> str:
    """'H2 v1' for the desk's ledger entry (cached; the ledger rarely changes)."""
    try:
        from .research import ledger, panel
        m = ledger.PATH.stat().st_mtime
        hit = _label_cache.get(desk)
        if hit and hit[0] == m:
            return hit[1]
        data = panel.ledger_data()
        h = (next((x for x in data.get("hypotheses", []) if x["id"] == desk.split(":", 1)[1]), None)
             if ":" in desk else panel.primary(data, desk))
        label = f"{h['id']} v{h.get('version', 1)}" if h else desk
        _label_cache[desk] = (m, label)
        return label
    except Exception:
        return desk


def write(desk: str, entry: dict, t=None, root=None) -> None:
    try:
        when = _ts(t if t is not None else entry.get("t"))
        rec = {"t": when.isoformat(timespec="seconds"), "desk": desk,
               "strategy": entry.pop("strategy", None) or strategy_label(desk), **entry}
        d = _dir(desk, root)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"{when:%Y-%m}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
    except Exception:                      # the journal must never stop a trade decision
        pass


def _files(desk: str, root=None) -> list[Path]:
    d = _dir(desk, root)
    return sorted(d.glob("*.jsonl")) if d.exists() else []


def read_since(desk: str, since: datetime, root=None) -> list[dict]:
    out = []
    for f in _files(desk, root):
        if f.stem < f"{since:%Y-%m}":
            continue
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if _ts(e.get("t")) >= since:
                    out.append(e)
    return out


def tail(desk: str, n: int = 10, root=None) -> list[dict]:
    """The newest n entries, newest first (reads only the end of the latest files)."""
    out: list[dict] = []
    for f in reversed(_files(desk, root)):
        try:
            size = os.path.getsize(f)
            with open(f, "rb") as fh:
                fh.seek(max(0, size - 64_000))
                lines = fh.read().decode("utf-8", "ignore").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
            if len(out) >= n:
                return out
    return out
