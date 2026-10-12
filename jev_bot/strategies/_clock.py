"""Bar times for intraday strategies. MT5 bars are stamped in the broker's server time,
not UTC, so strategies here never assume a time zone: they find trading breaks (the
daily pause and the weekend) from the gaps between bar times instead."""

from __future__ import annotations

from datetime import datetime

_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M")


def when(bar) -> datetime | None:
    for f in _FORMATS:
        try:
            return datetime.strptime(bar.date, f)
        except ValueError:
            continue
    return None


def gap_hours(prev, bar) -> float | None:
    """Hours between two bars' start times (1.0 = consecutive 1-hour bars)."""
    a, b = when(prev), when(bar)
    return (b - a).total_seconds() / 3600 if a and b else None
