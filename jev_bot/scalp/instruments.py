"""What the scalper can trade, and what each trade costs.

Costs are the whole game in scalping, so every instrument carries them:
  - the live bid/ask spread (taken from the feed on every tick)
  - crypto: an exchange fee per side (ScalpConfig.crypto_fee)
  - forex/gold: an optional commission per 100,000 units per side
    (ScalpConfig.fx_commission_per_100k: 0 for "standard" MT5 accounts whose
     cost is all in the spread, a few USD for "raw spread" accounts)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Spec:
    symbol: str          # our name, e.g. BTCUSDT, EURUSD, XAUUSD
    kind: str            # "crypto", "fx" or "metal"
    source: str          # "binance" or "mt5"
    digits: int          # price decimals for display
    pip: float           # 1 pip, for display (crypto: 1 tick of the quote)
    max_leverage: float = 10.0         # notional / equity cap for one position

    @property
    def base(self) -> str:
        return self.symbol[:3] if self.kind != "crypto" else self.symbol.replace("USDT", "")

    @property
    def quote(self) -> str:
        return "USD" if self.kind == "crypto" else self.symbol[3:6]


def _crypto(sym, digits):
    return Spec(sym, "crypto", "binance", digits, 10 ** -digits, max_leverage=3.0)


def _fx(sym, digits, pip):
    return Spec(sym, "fx", "mt5", digits, pip, max_leverage=20.0)


CRYPTO_MAJORS = {s.symbol: s for s in [
    _crypto("BTCUSDT", 2), _crypto("ETHUSDT", 2), _crypto("SOLUSDT", 2), _crypto("XRPUSDT", 4),
    _crypto("BNBUSDT", 2), _crypto("DOGEUSDT", 5), _crypto("ADAUSDT", 4), _crypto("AVAXUSDT", 2),
    _crypto("LINKUSDT", 3), _crypto("LTCUSDT", 2),
]}

FX_MAJORS = {s.symbol: s for s in [
    _fx("EURUSD", 5, 0.0001), _fx("GBPUSD", 5, 0.0001), _fx("USDJPY", 3, 0.01),
    _fx("USDCHF", 5, 0.0001), _fx("AUDUSD", 5, 0.0001), _fx("USDCAD", 5, 0.0001),
    _fx("NZDUSD", 5, 0.0001), _fx("EURGBP", 5, 0.0001),
    Spec("XAUUSD", "metal", "mt5", 2, 0.1, max_leverage=10.0),
]}

ALL = {**CRYPTO_MAJORS, **FX_MAJORS}


def get(symbol: str) -> Spec:
    s = symbol.upper().replace("/", "")
    if s in ALL:
        return ALL[s]
    if s.endswith("USDT"):                      # any other Binance USDT pair
        return _crypto(s, 4)
    if len(s) == 6 and s.isalpha():             # any other 6-letter forex pair
        return _fx(s, 3 if s.endswith("JPY") else 5, 0.01 if s.endswith("JPY") else 0.0001)
    raise SystemExit(f"unknown instrument {symbol!r}")


def quote_to_usd(spec: Spec, prices: dict) -> float | None:
    """Multiply an amount in the instrument's quote currency by this to get USD.
    `prices` maps symbol -> mid price. None if the needed rate is missing."""
    q = spec.quote
    if q in ("USD", "USDT"):
        return 1.0
    if spec.symbol.startswith("USD") and spec.symbol in prices:      # e.g. USDJPY: JPY -> USD
        return 1.0 / prices[spec.symbol]
    if q + "USD" in prices:                                         # e.g. EURGBP: GBP -> USD via GBPUSD
        return prices[q + "USD"]
    if "USD" + q in prices:
        return 1.0 / prices["USD" + q]
    return None
