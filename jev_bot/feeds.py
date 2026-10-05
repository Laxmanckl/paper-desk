"""Real market data: daily history and the latest price, from the internet.

Two sources, standard library only (no pip install):

    yahoo        free, no key.  EURUSD -> "EURUSD=X", XAUUSD -> "GC=F"
                 Note: Yahoo has no spot-gold ticker, so gold uses COMEX gold
                 futures (GC=F). It tracks spot XAU/USD closely but usually sits
                 a few dollars above it. Unofficial API: it can change or
                 rate-limit without notice.

    twelvedata   free tier with an API key (twelvedata.com). Real spot "XAU/USD"
                 and "EUR/USD". Set TWELVEDATA_API_KEY in your environment.
                 The free plan allows a limited number of calls per day.

Each source gives back the same thing: a list of daily fx.Bar (oldest first)
and a Quote (the latest price and when it was quoted). The rest of the bot
cannot tell which source ran.

Downloaded history is also saved to data/<SYMBOL>_<source>.csv, so you can
re-run a backtest on it later without downloading again.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .fx import Bar, INSTRUMENTS, resolve

TICKERS = {
    "yahoo": {"XAUUSD": "GC=F", "EURUSD": "EURUSD=X"},
    "twelvedata": {"XAUUSD": "XAU/USD", "EURUSD": "EUR/USD"},
}
SOURCES = tuple(TICKERS)

_UA = {"User-Agent": "Mozilla/5.0 (jev-bot paper-trading research)"}


@dataclass
class Quote:
    symbol: str
    price: float
    time: datetime          # when the price was quoted (UTC)
    source: str
    utc_offset_s: int = 0   # the exchange's clock vs UTC; daily bars are dated on it

    def trading_date(self) -> str:
        """The quote's calendar day on the exchange's clock (matches bar dates)."""
        return (self.time + timedelta(seconds=self.utc_offset_s)).strftime("%Y-%m-%d")

    def age_minutes(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (now - self.time).total_seconds() / 60


class FeedError(RuntimeError):
    pass


def _get_json(url: str, timeout: int = 20) -> dict:
    try:
        with urlopen(Request(url, headers=_UA), timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        raise FeedError(f"HTTP {e.code} from {url.split('?')[0]}") from None
    except URLError as e:
        raise FeedError(f"could not reach {url.split('?')[0]}: {e.reason}") from None


# --- yahoo -----------------------------------------------------------------

def yahoo_url(symbol: str, rng: str = "5y") -> str:
    t = TICKERS["yahoo"][symbol]
    return (f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?"
            + urlencode({"range": rng, "interval": "1d"}))


def parse_yahoo(symbol: str, data: dict) -> tuple[list[Bar], Quote]:
    try:
        res = data["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        err = (data.get("chart") or {}).get("error") if isinstance(data, dict) else None
        raise FeedError(f"yahoo returned no data for {symbol}: {err}")
    q = res["indicators"]["quote"][0]
    meta = res.get("meta", {})
    # Yahoo stamps a daily bar at midnight on the EXCHANGE's clock (forex: London,
    # e.g. 23:00 UTC in summer). Date it on that clock, not UTC, or every bar
    # lands one day early.
    offset = int(meta.get("gmtoffset") or 0)
    digits = INSTRUMENTS[symbol]["digits"]
    bars = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c = (q[k][i] for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c):
            continue                              # holiday / missing row
        day = datetime.fromtimestamp(ts + offset, timezone.utc).strftime("%Y-%m-%d")
        bar = Bar(day, round(o, digits), round(h, digits), round(l, digits), round(c, digits))
        if bars and bars[-1].date == day:         # yahoo sometimes repeats today
            bars[-1] = bar
        else:
            bars.append(bar)
    price = meta.get("regularMarketPrice") or (bars[-1].close if bars else None)
    when = meta.get("regularMarketTime")
    if price is None:
        raise FeedError(f"yahoo returned no price for {symbol}")
    quote = Quote(symbol, round(float(price), digits),
                  datetime.fromtimestamp(when, timezone.utc) if when else datetime.now(timezone.utc),
                  "yahoo", offset)
    return bars, quote


# --- twelve data -----------------------------------------------------------

def twelvedata_url(symbol: str, outputsize: int = 1300) -> str:
    key = os.environ.get("TWELVEDATA_API_KEY")
    if not key:
        raise FeedError("set TWELVEDATA_API_KEY to use --source twelvedata "
                        "(free key at twelvedata.com), or use --source yahoo")
    return ("https://api.twelvedata.com/time_series?"
            + urlencode({"symbol": TICKERS["twelvedata"][symbol], "interval": "1day",
                         "outputsize": outputsize, "timezone": "UTC", "apikey": key}))


def parse_twelvedata(symbol: str, data: dict) -> tuple[list[Bar], Quote]:
    if data.get("status") == "error" or "values" not in data:
        raise FeedError(f"twelvedata: {data.get('message', 'no data')}")
    digits = INSTRUMENTS[symbol]["digits"]
    bars = [Bar(v["datetime"][:10], round(float(v["open"]), digits),
                round(float(v["high"]), digits), round(float(v["low"]), digits),
                round(float(v["close"]), digits))
            for v in reversed(data["values"])]    # API returns newest first
    # the last daily bar's close is the latest price while the day is in progress
    quote = Quote(symbol, bars[-1].close, datetime.now(timezone.utc), "twelvedata")
    return bars, quote


# --- one entry point -------------------------------------------------------

def fetch(symbol: str, source: str = "yahoo", rng: str = "5y",
          save_dir: str | None = "data") -> tuple[list[Bar], Quote]:
    """Daily history (oldest first) and the latest quote for one instrument."""
    symbol = resolve(symbol)
    if source == "yahoo":
        bars, quote = parse_yahoo(symbol, _get_json(yahoo_url(symbol, rng)))
    elif source == "twelvedata":
        bars, quote = parse_twelvedata(symbol, _get_json(twelvedata_url(symbol)))
    else:
        raise FeedError(f"unknown source {source!r}; use one of {', '.join(SOURCES)}")
    if len(bars) < 60:
        raise FeedError(f"{source} returned only {len(bars)} daily bars for {symbol}")
    if save_dir:
        save_csv(bars, os.path.join(save_dir, f"{symbol}_{source}.csv"))
    return bars, quote


def save_csv(bars: list[Bar], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as fh:
        fh.write("date,open,high,low,close\n")
        for b in bars:
            fh.write(f"{b.date},{b.open},{b.high},{b.low},{b.close}\n")
