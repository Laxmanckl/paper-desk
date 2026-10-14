"""Simulated bridge for tests and local demos. Produces the exact payloads the
Windows MT5 bridge sends: closed M5 bars + the latest quote per symbol.
Random walks with realistic price levels and spreads; not a market model."""
from __future__ import annotations

import math
import random

from .instruments import INSTRUMENTS
from .types import BAR_SECONDS

START = {"XAUUSD": 2650.0, "EURUSD": 1.0850, "GBPUSD": 1.2950, "AUDUSD": 0.6650,
         "USDJPY": 149.50, "USDCAD": 1.3750, "USDCHF": 0.8650, "NZDUSD": 0.6050}
SPREAD_PIPS = {"XAUUSD": 2.5, "EURUSD": 0.8, "GBPUSD": 1.2, "AUDUSD": 1.0,
               "USDJPY": 1.0, "USDCAD": 1.5, "USDCHF": 1.4, "NZDUSD": 1.6}


class SimBridge:
    def __init__(self, seed: int = 1, start_ts: float = 1_760_000_000.0):
        """start_ts: where simulated history begins (first bar opens there)."""
        self.rng = random.Random(seed)
        self.t = start_ts - start_ts % BAR_SECONDS  # next bar to generate opens here
        self.px = dict(START)
        self.usd_drift = 0.0

    def _bar(self, sym: str) -> list:
        spec, rng = INSTRUMENTS[sym], self.rng
        vol = 0.00045 if spec["kind"] == "gold" else 0.00025
        o = self.px[sym]
        c = o * math.exp(rng.gauss(spec["usd_sign"] * self.usd_drift, vol))
        hi = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
        self.px[sym] = c
        point = 10 ** -spec["digits"]
        spread_pts = round(SPREAD_PIPS[sym] * spec["pip"] / point * rng.uniform(0.8, 1.3))
        d = spec["digits"]
        return [self.t, round(o, d), round(hi, d), round(lo, d), round(c, d), rng.randint(80, 900), spread_pts]

    def step_to(self, now: float) -> dict:
        """Generate every bar that has closed by `now`; return one bridge
        payload with those bars and a quote stamped just before `now`."""
        rows = {s: [] for s in INSTRUMENTS}
        while self.t + BAR_SECONDS <= now:
            if self.rng.random() < 0.02:
                self.usd_drift = self.rng.choice([0.0, 0.00006, -0.00006])
            for s in INSTRUMENTS:
                rows[s].append(self._bar(s))
            self.t += BAR_SECONDS
        syms = {}
        for s, spec in INSTRUMENTS.items():
            half = SPREAD_PIPS[s] * spec["pip"] / 2 * self.rng.uniform(0.8, 1.4)
            m = self.px[s]
            syms[s] = {"broker_symbol": s, "point": 10 ** -spec["digits"],
                       "quote": {"t": now - 1, "bid": round(m - half, spec["digits"]), "ask": round(m + half, spec["digits"])},
                       "bars": rows[s]}
        return {"sent": now, "broker": "Simulated broker", "server": "sim", "account": "demo",
                "version": "sim", "utc_offset_h": 0, "symbols": syms}
