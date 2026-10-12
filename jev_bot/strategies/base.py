"""The one shape every daily strategy takes, so the backtester and the live desk
can run any of them the same way.

    entry rules     signal(history) -> "BUY", "SELL" or None
                    history is every COMPLETED daily bar up to now; history[-1] is
                    the bar that just closed. The future is never passed in, so a
                    strategy cannot peek at it.
    exit rules      sl_atr / tp_atr: stop and target in ATR(14); max_hold days;
                    an opposite signal reverses the position
    sizing          risk_pct of equity lost if the stop is hit (capped by max_leverage)
    risk limits     the kill switch in config/risk.json, always, on top

Write a new one by copying _template.py. Then:

    python -m jev_bot research check <name>          unit tests + no-peeking checks
    python -m jev_bot research wire H4 daily --strategy <name>
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import backtest


@dataclass
class DailyStrategy:
    name: str = "unnamed"
    version: int = 1
    hypothesis: str = ""              # the one-line idea it tests
    markets: tuple = ("XAUUSD", "EURUSD")
    risk_pct: float = 0.01
    sl_atr: float = 1.5
    tp_atr: float = 3.0
    max_hold: int = 20
    max_leverage: float = 10.0
    needs_real_dates: bool = False    # trades on calendar events: simulated data has no event days
    rare: bool = False                # set-ups so rare that simulated data may never show one
    warmup: int = 0                   # bars of history before the first trade (0 = the usual 50)
    timeframe: str = "D1"             # bar size it runs on: D1, H4 or H1 (max_hold counts these bars)
    entry_spread_mult: float = 1.0    # entries happen when spreads are wider than usual (e.g. the Sunday reopen)
    history_bars: int = 0             # bars signal() needs to see (0 = all history); keeps long
                                      # hourly backtests fast: each call gets only the last `history_bars` bars

    # --- entry rules --------------------------------------------------------
    def signal(self, history: list) -> str | None:
        raise NotImplementedError

    # --- exit levels (optional) ----------------------------------------------
    def levels(self, history: list, side: str, entry: float, atr: float):
        """Exact (stop distance, target distance) in price units for this trade, or None
        to use sl_atr / tp_atr. For rules like "stop 1 ATR below the swing low". Uses only
        `history` (completed bars up to the signal), never the future."""
        return None

    # --- for the journal ----------------------------------------------------
    def conditions(self, history: list) -> dict:
        """Numbers worth recording with each decision (indicator values etc.)."""
        return {}

    def explain(self, history: list) -> str:
        """One plain sentence: why the signal is what it is."""
        return "rule fired" if self.signal(history) else "no rule fired"

    # --- control runs (optional) -----------------------------------------------------
    def controls(self) -> list:
        """[(label, strategy), ...]: the same trade where the claimed edge should NOT be
        (another time of day, say). `research control <id>` runs them on the development
        period only; if they do as well, the edge is the market's drift, not the idea."""
        return []

    # --- unit tests -----------------------------------------------------------
    def tests(self) -> list:
        """[(description, history, expected signal), ...] checked by `research check`."""
        return []

    # --- shared -----------------------------------------------------------------
    def settings(self) -> backtest.Settings:
        return backtest.Settings(risk_pct=self.risk_pct, sl_atr=self.sl_atr, tp_atr=self.tp_atr,
                                 max_hold=self.max_hold, max_leverage=self.max_leverage)

    @property
    def label(self) -> str:
        return f"jev_bot/strategies/{self.name}.py v{self.version}"
