"""Economic-calendar blackout filter.

``data/news_blackout.csv`` holds one row per high-impact release::

    datetime,currency,event
    2024-01-05 13:30,USD,NFP
    2024-01-11 13:30,USD,CPI

``datetime`` is **UTC** (the file stores the release time, not the broker's
server time).  New entries are blocked from ``window_minutes`` before the
release until ``window_minutes`` after it (default ±30 min).  Existing
positions are *not* closed — the filter only gates new entries, which is what
the brief asks for and avoids turning a news spike into a forced market exit.

Refreshing the file is manual by design (release calendars move): see
``scripts/build_news_blackout.py`` and the README section "News blackout".
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

log = logging.getLogger("bot.news")

__all__ = ["NewsEvent", "NewsFilter", "load_news_filter", "symbol_currencies"]

DEFAULT_NEWS_FILE = Path(__file__).resolve().parent.parent / "data" / "news_blackout.csv"

# currency exposure of each instrument (both legs matter for FX)
_FALLBACK_CURRENCIES = {
    "XAUUSD": ("USD",),
    "XAGUSD": ("USD",),
    "EURUSD": ("EUR", "USD"),
    "GBPUSD": ("GBP", "USD"),
    "AUDUSD": ("AUD", "USD"),
    "NZDUSD": ("NZD", "USD"),
    "USDCAD": ("USD", "CAD"),
    "USDCHF": ("USD", "CHF"),
    "USDJPY": ("USD", "JPY"),
    "EURGBP": ("EUR", "GBP"),
    "EURJPY": ("EUR", "JPY"),
    "GBPJPY": ("GBP", "JPY"),
}


def symbol_currencies(symbol: str) -> tuple[str, ...]:
    """Currencies whose releases matter for ``symbol``."""
    sym = symbol.upper().replace("/", "").replace(".", "")
    if sym in _FALLBACK_CURRENCIES:
        return _FALLBACK_CURRENCIES[sym]
    if sym.startswith(("XAU", "XAG", "XPT")):
        return ("USD",)
    if len(sym) == 6:
        return (sym[:3], sym[3:])
    return ()


@dataclass(frozen=True)
class NewsEvent:
    time: pd.Timestamp
    currency: str
    event: str = ""


@dataclass
class NewsFilter:
    """Blocks entries within ``window_minutes`` of a listed release."""

    events: list[NewsEvent] = field(default_factory=list)
    window_minutes: int = 30
    source: str = "none"

    def __post_init__(self) -> None:
        self.events = sorted(self.events, key=lambda e: e.time)
        self._by_currency: dict[str, list[pd.Timestamp]] = {}
        for ev in self.events:
            self._by_currency.setdefault(ev.currency.upper(), []).append(ev.time)
        self._labels: dict[pd.Timestamp, str] = {
            ev.time: f"{ev.currency} {ev.event}".strip() for ev in self.events
        }

    def __len__(self) -> int:
        return len(self.events)

    def blocked(self, ts, currencies: tuple[str, ...] | list[str] | None
                ) -> tuple[bool, str]:
        """``(blocked, reason)`` for an entry at ``ts`` (UTC)."""
        if not self.events or ts is None:
            return False, ""
        when = pd.Timestamp(ts)
        if when.tzinfo is not None:
            when = when.tz_convert("UTC").tz_localize(None)
        window = pd.Timedelta(minutes=int(self.window_minutes))
        for cur in (currencies or ()):
            stamps = self._by_currency.get(str(cur).upper())
            if not stamps:
                continue
            for stamp in stamps:
                if stamp - window <= when <= stamp + window:
                    label = self._labels.get(stamp, str(cur))
                    minutes = (when - stamp).total_seconds() / 60.0
                    return True, (f"news blackout: {label} at "
                                  f"{stamp:%Y-%m-%d %H:%M} UTC ({minutes:+.0f} min)")
        return False, ""


def load_news_filter(path: str | Path | None = DEFAULT_NEWS_FILE,
                     window_minutes: int = 30,
                     enabled: bool = True) -> NewsFilter:
    """Load the blackout CSV.  A missing file means "no filter", never a crash."""
    if not enabled:
        return NewsFilter(window_minutes=window_minutes, source="disabled")
    if path is None:
        return NewsFilter(window_minutes=window_minutes, source="none")
    p = Path(path)
    if not p.exists():
        log.warning("news blackout file %s not found — news filter inactive", p)
        return NewsFilter(window_minutes=window_minutes, source=f"missing:{p}")

    events: list[NewsEvent] = []
    with open(p, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            raw = (row.get("datetime") or "").strip()
            cur = (row.get("currency") or "").strip().upper()
            if not raw or not cur:
                continue
            try:
                when = pd.Timestamp(raw)
            except (ValueError, TypeError):
                log.warning("news blackout: bad datetime %r — skipped", raw)
                continue
            events.append(NewsEvent(time=when, currency=cur,
                                    event=(row.get("event") or "").strip()))
    return NewsFilter(events=events, window_minutes=window_minutes, source=str(p))
