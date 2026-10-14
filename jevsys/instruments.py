"""The instruments the desk trades: gold + the seven forex majors.

pip: the price step traders quote moves in (0.0001 for most pairs, 0.01 for
JPY pairs, 0.1 for gold). usd_sign: +1 if the pair rises when the US dollar
strengthens (USDJPY), -1 if it falls (EURUSD, gold). Used for the USD index.
"""
from __future__ import annotations

INSTRUMENTS: dict[str, dict] = {
    "XAUUSD": {"kind": "gold", "pip": 0.1, "digits": 2, "usd_sign": -1},
    "EURUSD": {"kind": "fx", "pip": 0.0001, "digits": 5, "usd_sign": -1},
    "GBPUSD": {"kind": "fx", "pip": 0.0001, "digits": 5, "usd_sign": -1},
    "AUDUSD": {"kind": "fx", "pip": 0.0001, "digits": 5, "usd_sign": -1},
    "USDJPY": {"kind": "fx", "pip": 0.01, "digits": 3, "usd_sign": 1},
    "USDCAD": {"kind": "fx", "pip": 0.0001, "digits": 5, "usd_sign": 1},
    "USDCHF": {"kind": "fx", "pip": 0.0001, "digits": 5, "usd_sign": 1},
    "NZDUSD": {"kind": "fx", "pip": 0.0001, "digits": 5, "usd_sign": -1},
}

FX_MAJORS = [s for s, v in INSTRUMENTS.items() if v["kind"] == "fx"]

CONTRACT = {"gold": 100, "fx": 100_000}  # units per 1.00 lot (100 oz gold; 100,000 base currency)


def pip_value_usd(symbol: str, price: float) -> float:
    """US-dollar value of a one-pip move for 1.00 lot.
    XXX/USD and gold: fixed ($10). USD/XXX: converted at the current price."""
    spec = INSTRUMENTS[symbol]
    v = CONTRACT[spec["kind"]] * spec["pip"]
    return v if symbol.endswith("USD") else v / price


def pip(symbol: str) -> float:
    return INSTRUMENTS[symbol]["pip"]
