"""Backtest: walk the decision loop through history, bar by bar, on paper.

For every daily bar t (after a 50-bar warm-up):

    state    = fx.state_at(bars, t)          # only bars 0..t, no lookahead
    decision = jev.decide(state)             # BUY / SELL / HOLD / AVOID
    gate     = risk.check(decision)          # may it execute?

    EXECUTE + no position      -> open at the NEXT bar's open
    EXECUTE + opposite position -> close it, then open the new side
    anything else              -> keep managing the open position

An open position closes on its stop-loss, its take-profit, a time limit, or an
opposite executed signal. Stops are ATR-based. If a bar touches both the stop
and the target, the stop is assumed to have hit first (the pessimistic call).

Costs: the full spread is paid on every round trip (buy at ask, sell at bid).
Sizing: each trade risks a fixed fraction of equity on the distance to its stop,
the way most forex traders size, with a leverage cap.

Paper only. One account per instrument, starting at $10,000.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import fx, jev, risk


@dataclass
class Settings:
    risk_pct: float = 0.01      # fraction of equity lost if the stop is hit
    sl_atr: float = 1.5         # stop-loss distance in ATR(14)
    tp_atr: float = 3.0         # take-profit distance in ATR(14)
    max_hold: int = 20          # bars before a position is closed regardless
    max_leverage: float = 10.0  # notional / equity cap
    start_equity: float = 10_000.0


@dataclass
class Trade:
    side: str
    entry_date: str
    entry: float
    stop: float
    target: float
    units: float
    exit_date: str = ""
    exit: float = 0.0
    reason: str = ""
    pnl: float = 0.0
    pips: float = 0.0
    bars: int = 0


@dataclass
class Result:
    symbol: str
    source: str                 # "sim" or the csv path
    first_date: str
    last_date: str
    bars: int
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)
    buy_hold: float = 0.0
    start_equity: float = 10_000.0
    decisions: dict = field(default_factory=dict)   # action -> count
    executed_signals: int = 0

    # --- metrics -------------------------------------------------------------
    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1] if self.equity_curve else self.start_equity

    @property
    def total_return(self) -> float:
        return self.final_equity / self.start_equity - 1

    @property
    def wins(self):
        return [t for t in self.trades if t.pnl > 0]

    @property
    def losses(self):
        return [t for t in self.trades if t.pnl <= 0]

    @property
    def win_rate(self) -> float:
        return len(self.wins) / len(self.trades) if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        gross_win = sum(t.pnl for t in self.wins)
        gross_loss = -sum(t.pnl for t in self.losses)
        if gross_loss == 0:
            return float("inf") if gross_win > 0 else 0.0
        return gross_win / gross_loss

    @property
    def max_drawdown(self) -> float:
        peak, mdd = self.start_equity, 0.0
        for e in self.equity_curve:
            peak = max(peak, e)
            mdd = max(mdd, (peak - e) / peak)
        return mdd

    @property
    def total_pips(self) -> float:
        return sum(t.pips for t in self.trades)


def _close(tr: Trade, price: float, date: str, reason: str, t_open: int, t: int,
           spec: dict) -> Trade:
    half = spec["spread"] / 2
    fill = price - half if tr.side == "BUY" else price + half      # exit at bid / ask
    direction = 1 if tr.side == "BUY" else -1
    tr.exit, tr.exit_date, tr.reason = fill, date, reason
    tr.pnl = round((fill - tr.entry) * direction * tr.units, 2)
    tr.pips = round((fill - tr.entry) * direction / spec["pip"], 1)
    tr.bars = t - t_open
    return tr


def run(symbol: str, bars: list, settings: Settings | None = None,
        limits: risk.Limits | None = None, engine: str = "offline",
        source: str = "sim") -> Result:
    symbol = fx.resolve(symbol)
    s = settings or Settings()
    lim = limits or risk.Limits()
    spec = fx.INSTRUMENTS[symbol]
    half = spec["spread"] / 2

    res = Result(symbol=symbol, source=source, first_date=bars[fx.WARMUP].date,
                 last_date=bars[-1].date, bars=len(bars) - fx.WARMUP,
                 start_equity=s.start_equity)
    res.buy_hold = bars[-1].close / bars[fx.WARMUP].close - 1

    equity = s.start_equity
    pos: Trade | None = None
    t_open = 0
    pending = None          # side to open at the next bar's open

    for t in range(fx.WARMUP, len(bars)):
        b = bars[t]

        # 1. fill an order signalled on the previous bar, at this bar's open
        if pending and pos is None:
            a = fx.atr(bars, t - 1)
            entry = b.open + half if pending == "BUY" else b.open - half
            stop_dist = s.sl_atr * a
            units = (equity * s.risk_pct) / stop_dist if stop_dist > 0 else 0
            units = min(units, equity * s.max_leverage / entry)
            sign = 1 if pending == "BUY" else -1
            pos = Trade(side=pending, entry_date=b.date, entry=entry,
                        stop=entry - sign * stop_dist,
                        target=entry + sign * s.tp_atr * a, units=units)
            t_open = t
        pending = None

        # 2. manage the open position against this bar's range
        if pos is not None:
            hit = None
            if pos.side == "BUY":
                if b.low <= pos.stop:
                    hit = (min(pos.stop, b.open), "stop")      # gap through the stop
                elif b.high >= pos.target:
                    hit = (max(pos.target, b.open), "target")
            else:
                if b.high >= pos.stop:
                    hit = (max(pos.stop, b.open), "stop")
                elif b.low <= pos.target:
                    hit = (min(pos.target, b.open), "target")
            if hit is None and t - t_open >= s.max_hold:
                hit = (b.close, "time")
            if hit:
                # the stop/target levels already include the spread at entry; the
                # exit side is charged in _close
                tr = _close(pos, hit[0], b.date, hit[1], t_open, t, spec)
                equity += tr.pnl
                res.trades.append(tr)
                pos = None

        # 3. decide on this bar's close
        state = fx.state_at(symbol, bars, t)
        d = jev.decide(state, engine=engine)
        res.decisions[d.action] = res.decisions.get(d.action, 0) + 1
        # one position per instrument is enforced here, so the gate's position
        # cap is not the binding rule; confidence, probability and action are
        gate = risk.check(d, 0, lim)
        if gate.verdict == "EXECUTE" and t < len(bars) - 1:
            if pos is None:
                pending = d.action
                res.executed_signals += 1
            elif pos.side != d.action:                     # reverse on an opposite signal
                tr = _close(pos, b.close, b.date, "reverse", t_open, t, spec)
                equity += tr.pnl
                res.trades.append(tr)
                pos = None
                pending = d.action
                res.executed_signals += 1

        # 4. mark to market
        if pos is not None:
            direction = 1 if pos.side == "BUY" else -1
            mark = b.close - half if pos.side == "BUY" else b.close + half
            res.equity_curve.append(round(equity + (mark - pos.entry) * direction * pos.units, 2))
        else:
            res.equity_curve.append(round(equity, 2))

    # close anything still open on the last bar
    if pos is not None:
        tr = _close(pos, bars[-1].close, bars[-1].date, "end", t_open, len(bars) - 1, spec)
        equity += tr.pnl
        res.trades.append(tr)
        res.equity_curve[-1] = round(equity, 2)
    return res
