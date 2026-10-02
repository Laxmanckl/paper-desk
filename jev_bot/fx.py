"""Forex and gold: XAUUSD and EURUSD.

This module adds two instruments to jev-bot and turns their price history into
the same MarketState shape the decision loop already consumes:

    XAUUSD   gold quoted in US dollars       (asset_class "commodity")
    EURUSD   euro quoted in US dollars       (asset_class "forex")

Two data sources, both offline:

- simulate(): a seeded, regime-switching price generator (trending up, trending
  down, ranging) with per-instrument volatility, so runs are reproducible.
- load_csv(): real daily history you download yourself (Yahoo Finance,
  Investing.com, a MetaTrader export, your broker). Columns are matched by
  name: date, open, high, low, close (Investing.com's "Price" counts as close).

Feature mapping (state at bar t uses bars 0..t only, never the future):

    change_24h    close[t] / close[t-1] - 1
    momentum      tanh((EMA10 - EMA30) / ATR14 / 1.5)      in -1..1
    regime        close vs SMA50 and the SMA50 slope
    volume_delta  0.0  (spot FX has no central volume; left neutral on purpose)
    news          0.0  (no news feed wired; plug one into `news_score`)

Everything here is for testing. Nothing places an order.
"""

from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass
from datetime import datetime

from .types import MarketState

# symbol -> instrument spec. Start prices are simulation seeds, not quotes.
INSTRUMENTS = {
    "XAUUSD": dict(name="Gold / US Dollar", asset_class="commodity",
                   start=3300.0, daily_vol=0.010, pip=0.10, spread=0.30, digits=2),
    "EURUSD": dict(name="Euro / US Dollar", asset_class="forex",
                   start=1.1500, daily_vol=0.0045, pip=0.0001, spread=0.00010, digits=5),
}

ALIASES = {"GOLD": "XAUUSD", "XAU": "XAUUSD", "XAU/USD": "XAUUSD",
           "EUR/USD": "EURUSD", "USD/EUR": "EURUSD", "USDEUR": "EURUSD", "EUR": "EURUSD"}


def resolve(symbol: str) -> str:
    s = symbol.upper().strip()
    s = ALIASES.get(s, s)
    if s not in INSTRUMENTS:
        raise SystemExit(f"unknown instrument {symbol!r}. use one of: "
                         + ", ".join(INSTRUMENTS))
    return s


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float


# --- simulated history ---------------------------------------------------------

# regime -> (daily drift as a fraction of daily vol, vol multiplier)
_REGIME_DRIFT = {"up": (0.12, 1.0), "down": (-0.12, 1.1), "range": (0.0, 0.8)}


def simulate(symbol: str, bars: int = 750, seed: int = 7, trends: bool = True) -> list[Bar]:
    """A daily OHLC series with trending and ranging stretches.

    Regimes last 20-120 bars. Within a regime, returns are normal with a small
    drift. It is a test harness, not a model of the real market: real gold and
    EURUSD have fat tails, gaps and news shocks this does not reproduce.

    trends=False gives a pure random walk: no edge exists to be found, so a
    strategy that still "wins" there is fitting noise. Use it as a control.
    """
    spec = INSTRUMENTS[resolve(symbol)]
    rng = random.Random(f"{symbol}-{seed}")
    vol = spec["daily_vol"]
    price = spec["start"]
    out: list[Bar] = []
    regime, left = "range", 0
    for i in range(bars):
        if left <= 0:
            regime = rng.choices(["up", "down", "range"], weights=[0.35, 0.3, 0.35])[0]
            left = rng.randint(20, 120)
        left -= 1
        drift, vmult = _REGIME_DRIFT[regime] if trends else (0.0, 1.0)
        sigma = vol * vmult
        ret = rng.gauss(drift * sigma, sigma)
        if rng.random() < 0.02:                      # occasional shock day
            ret += rng.choice([-1, 1]) * rng.uniform(2, 4) * sigma
        o = price * (1 + rng.gauss(0, sigma * 0.1))
        c = o * (1 + ret)
        hi = max(o, c) * (1 + abs(rng.gauss(0, sigma * 0.5)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, sigma * 0.5)))
        d = spec["digits"]
        out.append(Bar(f"sim-{i:04d}", round(o, d), round(hi, d), round(lo, d), round(c, d)))
        price = c
    return out


# --- real history from a CSV ---------------------------------------------------

_DATE_FORMATS = ("%Y-%m-%d", "%Y.%m.%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%m-%Y",
                 "%b %d, %Y", "%Y%m%d", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M")


def _parse_dates(values: list[str]):
    """Pick the one format that parses every date in the file, so 03/04/2024
    is read consistently (day-first or month-first, decided by the whole file).
    Returns None if no single format fits; the file order is then kept."""
    for f in _DATE_FORMATS:
        try:
            return [datetime.strptime(v.strip(), f) for v in values]
        except ValueError:
            continue
    return None


def _num(s: str) -> float:
    return float(str(s).replace(",", "").replace("$", "").strip())


def load_csv(path: str) -> list[Bar]:
    """Read daily bars from a CSV. Sorted oldest first, whatever the file order."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(fh, dialect)
        header = [h.strip().strip("<>").lower() for h in next(reader)]

        def col(*names):
            for n in names:
                if n in header:
                    return header.index(n)
            return None

        i_date = col("date", "datetime", "time", "timestamp")
        i_close = col("close", "price", "adj close", "last")
        i_open, i_high, i_low = col("open"), col("high"), col("low")
        if i_date is None or i_close is None:
            raise SystemExit(f"{path}: need at least a date and a close/price column; "
                             f"found {header}")
        rows = []
        for row in reader:
            if not row or len(row) <= max(i_date, i_close):
                continue
            try:
                c = _num(row[i_close])
            except ValueError:
                continue                      # skips "null" rows (Yahoo holidays)
            o = _num(row[i_open]) if i_open is not None and row[i_open].strip() else c
            h = _num(row[i_high]) if i_high is not None and row[i_high].strip() else max(o, c)
            l = _num(row[i_low]) if i_low is not None and row[i_low].strip() else min(o, c)
            rows.append((row[i_date].strip(), o, h, l, c))

    parsed = _parse_dates([r[0] for r in rows])
    if parsed:
        rows = [r for _, r in sorted(zip(parsed, rows), key=lambda x: x[0])]
    if len(rows) < 60:
        raise SystemExit(f"{path}: only {len(rows)} usable rows; need at least 60 daily bars")
    return [Bar(*r) for r in rows]


# --- features ------------------------------------------------------------------

WARMUP = 50     # bars needed before the first state (SMA50)


def _ema(vals: list[float], n: int) -> float:
    k = 2 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def atr(bars: list[Bar], t: int, n: int = 14) -> float:
    trs = []
    for i in range(max(1, t - n + 1), t + 1):
        b, prev = bars[i], bars[i - 1].close
        trs.append(max(b.high - b.low, abs(b.high - prev), abs(b.low - prev)))
    return sum(trs) / len(trs) if trs else 0.0


def news_score(symbol: str, date: str) -> float:
    """Hook for a news / macro sentiment feed (-1..1). Neutral until wired."""
    return 0.0


def state_at(symbol: str, bars: list[Bar], t: int) -> MarketState:
    """The MarketState for bar t, built only from bars[0..t]."""
    if t < WARMUP:
        raise ValueError(f"need {WARMUP} bars of history before the first state")
    spec = INSTRUMENTS[symbol]
    closes = [b.close for b in bars[max(0, t - 120):t + 1]]
    a = atr(bars, t) or 1e-12
    momentum = math.tanh((_ema(closes, 10) - _ema(closes, 30)) / a / 1.5)
    sma50 = sum(b.close for b in bars[t - 49:t + 1]) / 50
    sma50_prev = sum(b.close for b in bars[t - 59:t - 9]) / 50 if t >= 59 else sma50
    px = bars[t].close
    if px > sma50 and sma50 > sma50_prev:
        regime = "bullish"
    elif px < sma50 and sma50 < sma50_prev:
        regime = "bearish"
    else:
        regime = "neutral"
    return MarketState(
        symbol=symbol,
        asset_class=spec["asset_class"],
        price=px,
        change_24h=round(px / bars[t - 1].close - 1, 5),
        volume_delta=0.0,
        momentum=round(momentum, 3),
        news=news_score(symbol, bars[t].date),
        regime=regime,
    )


def snapshot(seed: int = 7, bars: int = 750, csvs: dict | None = None) -> list[MarketState]:
    """The latest state for each instrument, for the decisions / card commands."""
    out = []
    for sym in INSTRUMENTS:
        hist = load_csv(csvs[sym]) if csvs and sym in csvs else simulate(sym, bars, seed)
        out.append(state_at(sym, hist, len(hist) - 1))
    return out


def fmt_price(symbol: str, price: float) -> str:
    spec = INSTRUMENTS.get(symbol)
    if not spec:
        return f"${price:,.2f}"
    return f"{price:,.{spec['digits']}f}"
