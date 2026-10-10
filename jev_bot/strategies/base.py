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

    # --- entry rules --------------------------------------------------------
    def signal(self, history: list) -> str | None:
        raise NotImplementedError

    # --- for the journal ----------------------------------------------------
    def conditions(self, history: list) -> dict:
        """Numbers worth recording with each decision (indicator values etc.)."""
        return {}

    def explain(self, history: list) -> str:
        """One plain sentence: why the signal is what it is."""
        return "rule fired" if self.signal(history) else "no rule fired"

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
