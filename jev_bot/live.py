"""Live paper trading: real prices, fake money, saved between runs.

Each `check()`:

  1. downloads the latest daily history and current price (feeds.fetch)
  2. manages open positions against the REAL price:
       - stop-loss / take-profit hit?  (current price, plus the high/low of any
         full day completed since the trade was opened)
       - held longer than max_hold trading days? -> close
  3. once per new completed daily bar, asks the decision engine
     BUY / SELL / HOLD / AVOID, runs the risk gate, and opens / reverses a
     position at the current price (plus spread). Same rules as the backtest.
  4. saves everything to the account file (paper_account.json by default)

So you can run `live` once a day, or leave `live --watch 15` running, close it,
and pick up later: the account remembers its positions and history.

No broker, no order, no real money. Every fill is written to the account file
and is fake.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from . import backtest, feeds, fx, jev, risk

ACCOUNT_VERSION = 1


@dataclass
class Position:
    symbol: str
    side: str
    entry: float
    stop: float
    target: float
    units: float
    opened_at: str          # ISO time (UTC)
    entry_bar: str          # date of the last completed bar when it opened
    signal_prob: float
    signal_conf: float


@dataclass
class Account:
    start_equity: float = 10_000.0
    cash: float = 10_000.0                       # realised equity
    positions: dict = field(default_factory=dict)        # symbol -> Position dict
    closed: list = field(default_factory=list)           # closed trades
    last_bar: dict = field(default_factory=dict)         # symbol -> last decided bar date
    last_decision: dict = field(default_factory=dict)    # symbol -> summary
    last_price: dict = field(default_factory=dict)       # symbol -> {price, time}
    created: str = ""
    version: int = ACCOUNT_VERSION
    # for the dashboard
    equity_log: list = field(default_factory=list)       # [iso time, equity]
    events: list = field(default_factory=list)           # [iso time, text]
    closes: dict = field(default_factory=dict)           # symbol -> [[date, close], ...]
    config: dict = field(default_factory=dict)           # rules shown on the dashboard

    MAX_LOG = 3000
    MAX_EVENTS = 300

    def record(self, events: list, now: datetime | None = None) -> None:
        """Append this check's events and an equity point (kept to a bounded size)."""
        t = (now or _now()).isoformat(timespec="seconds")
        self.events.extend([t, e] for e in events)
        self.events = self.events[-self.MAX_EVENTS:]
        self.equity_log.append([t, self.equity()])
        if len(self.equity_log) > self.MAX_LOG:      # thin the oldest half, keep recent detail
            old, recent = self.equity_log[:-1000], self.equity_log[-1000:]
            self.equity_log = old[::2] + recent

    # --- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path: str, start_equity: float = 10_000.0) -> "Account":
        if not os.path.exists(path):
            return cls(start_equity=start_equity, cash=start_equity,
                       created=_now().isoformat(timespec="seconds"))
        with open(path) as fh:
            data = json.load(fh)
        acct = cls(**{k: v for k, v in data.items()
                      if k in cls.__dataclass_fields__ and not k.startswith("MAX")})
        return acct

    def save(self, path: str) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(asdict(self), fh, indent=2)
        os.replace(tmp, path)                     # never leaves a half-written file

    # --- numbers -----------------------------------------------------------
    def open_pnl(self) -> float:
        total = 0.0
        for sym, p in self.positions.items():
            px = (self.last_price.get(sym) or {}).get("price")
            if px is not None:
                total += _pnl(p, _exit_fill(p["side"], px, sym))
        return round(total, 2)

    def equity(self) -> float:
        return round(self.cash + self.open_pnl(), 2)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _half_spread(sym: str) -> float:
    return fx.INSTRUMENTS[sym]["spread"] / 2


def _exit_fill(side: str, price: float, sym: str) -> float:
    return price - _half_spread(sym) if side == "BUY" else price + _half_spread(sym)


def _pnl(p: dict, fill: float) -> float:
    direction = 1 if p["side"] == "BUY" else -1
    return round((fill - p["entry"]) * direction * p["units"], 2)


def _close(acct: Account, sym: str, price: float, reason: str, now: datetime) -> dict:
    p = acct.positions.pop(sym)
    fill = _exit_fill(p["side"], price, sym)
    pnl = _pnl(p, fill)
    direction = 1 if p["side"] == "BUY" else -1
    trade = dict(p, exit=fill, closed_at=now.isoformat(timespec="seconds"),
                 reason=reason, pnl=pnl,
                 pips=round((fill - p["entry"]) * direction / fx.INSTRUMENTS[sym]["pip"], 1))
    acct.cash = round(acct.cash + pnl, 2)
    acct.closed.append(trade)
    return trade


def _open(acct: Account, sym: str, side: str, price: float, a: float, bar_date: str,
          d, settings: backtest.Settings, now: datetime) -> dict:
    entry = price + _half_spread(sym) if side == "BUY" else price - _half_spread(sym)
    stop_dist = settings.sl_atr * a
    equity = acct.equity()
    units = (equity * settings.risk_pct) / stop_dist if stop_dist > 0 else 0
    units = min(units, equity * settings.max_leverage / entry)
    sign = 1 if side == "BUY" else -1
    p = Position(sym, side, entry, entry - sign * stop_dist,
                 entry + sign * settings.tp_atr * a, round(units, 6),
                 now.isoformat(timespec="seconds"), bar_date,
                 d.probability, d.confidence)
    acct.positions[sym] = asdict(p)
    return acct.positions[sym]


def completed_bars(bars: list, quote_time, utc_offset_s: int = 0) -> list:
    """Drop today's still-forming bar so decisions use finished days only,
    exactly as the backtest does. Accepts a Quote or a UTC datetime."""
    if hasattr(quote_time, "trading_date"):
        today = quote_time.trading_date()
    else:
        today = (quote_time + timedelta(seconds=utc_offset_s)).strftime("%Y-%m-%d")
    if bars and bars[-1].date >= today:
        return bars[:-1]
    return bars


def check(acct: Account, symbols: list[str], source: str = "yahoo",
          settings: backtest.Settings | None = None, limits: risk.Limits | None = None,
          engine: str = "offline", fetch=None, now: datetime | None = None) -> list[str]:
    """One pass over the instruments. Returns human-readable event lines."""
    s = settings or backtest.Settings()
    lim = limits or risk.Limits()
    fetch = fetch or (lambda sym: feeds.fetch(sym, source))
    events: list[str] = []

    for sym in symbols:
        sym = fx.resolve(sym)
        t_now = now or _now()
        try:
            bars, quote = fetch(sym)
        except feeds.FeedError as e:
            events.append(f"{sym}: data error, skipped this check ({e})")
            continue
        px, d_px = quote.price, fx.INSTRUMENTS[sym]["digits"]
        acct.last_price[sym] = {"price": px, "time": quote.time.isoformat(timespec="seconds"),
                                "source": quote.source}
        done = completed_bars(bars, quote)
        acct.closes[sym] = [[b.date, b.close] for b in done[-90:]]
        if len(done) <= fx.WARMUP:
            events.append(f"{sym}: only {len(done)} completed bars, need {fx.WARMUP + 1}")
            continue
        if quote.age_minutes(t_now) > 90:
            events.append(f"{sym}: market closed or quote stale "
                          f"(last {quote.time:%a %d %b %H:%M} UTC); positions held, no new trades")
            continue

        # 1. manage an open position against the real price
        p = acct.positions.get(sym)
        if p:
            hit = None
            # whole days completed AFTER the day the trade opened: their high/low
            # may have touched a level while the bot was not running. (The entry
            # day itself is skipped: its range may include moves before entry.)
            later = [b for b in done if b.date > p["opened_at"][:10]]
            for b in later:
                if p["side"] == "BUY":
                    if b.low <= p["stop"]:
                        hit = (min(p["stop"], b.open), "stop"); break    # stop first if both
                    if b.high >= p["target"]:
                        hit = (max(p["target"], b.open), "target"); break
                else:
                    if b.high >= p["stop"]:
                        hit = (max(p["stop"], b.open), "stop"); break
                    if b.low <= p["target"]:
                        hit = (min(p["target"], b.open), "target"); break
            if hit is None:
                if (p["side"] == "BUY" and px <= p["stop"]) or (p["side"] == "SELL" and px >= p["stop"]):
                    hit = (px, "stop")
                elif (p["side"] == "BUY" and px >= p["target"]) or (p["side"] == "SELL" and px <= p["target"]):
                    hit = (px, "target")
                elif len(later) >= s.max_hold:
                    hit = (px, "time")
            if hit:
                tr = _close(acct, sym, hit[0], hit[1], t_now)
                events.append(f"{sym}: CLOSED {tr['side']} at {tr['exit']:,.{d_px}f} "
                              f"({tr['reason']})  {tr['pips']:+.1f} pips  ${tr['pnl']:+,.2f}")

        # 2. decide once per new completed daily bar
        last = done[-1].date
        if acct.last_bar.get(sym) == last:
            continue
        state = fx.state_at(sym, done, len(done) - 1)
        d = jev.decide(state, engine=engine)
        gate = risk.check(d, 0, lim)
        acct.last_bar[sym] = last
        acct.last_decision[sym] = {"bar": last, "action": d.action, "probability": d.probability,
                                   "confidence": d.confidence, "gate": gate.verdict,
                                   "reason": gate.reason, "momentum": state.momentum,
                                   "regime": state.regime, "source": d.source}
        line = (f"{sym}: new daily bar {last} -> {d.action} "
                f"(p {d.probability:.0%}, conf {d.confidence:.0%}) gate {gate.verdict}"
                + (f" · {gate.reason}" if gate.reason else ""))
        if gate.verdict != "EXECUTE":
            events.append(line)
            continue
        p = acct.positions.get(sym)
        if p and p["side"] == d.action:
            events.append(line + " · already holding this side")
            continue
        if p:
            tr = _close(acct, sym, px, "reverse", t_now)
            events.append(f"{sym}: CLOSED {tr['side']} at {tr['exit']:,.{d_px}f} (reverse)  "
                          f"{tr['pips']:+.1f} pips  ${tr['pnl']:+,.2f}")
        a = fx.atr(done, len(done) - 1)
        np_ = _open(acct, sym, d.action, px, a, last, d, s, t_now)
        events.append(line)
        events.append(f"{sym}: OPENED {d.action} at {np_['entry']:,.{d_px}f}  "
                      f"stop {np_['stop']:,.{d_px}f}  target {np_['target']:,.{d_px}f}  "
                      f"size {np_['units']:,.2f} units")
    return events
