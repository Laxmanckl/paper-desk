"""Lorentzian Classification, ported from TradingView Pine Script to Python.

Original: "Machine Learning: Lorentzian Classification" by jdehorty (TradingView,
Mozilla Public License 2.0), with his MLExtensions v2 and KernelFunctions v2
libraries. The copy received was retitled "SmartAIML"; the logic is jdehorty's.

Streaming: feed one closed candle at a time with update(); every value depends
only on candles already fed (no look-ahead), the same way Pine runs bar by bar.

Default settings (the script's defaults):
    features      RSI(14), WaveTrend(10, 11), CCI(20), ADX(20), RSI(9), each scaled 0..1
    neighbours    8, searched among up to 2000 stored candles, every 4th skipped,
                  using Lorentzian distance: sum of log(1 + |difference|)
    label         per stored candle: -1 if price ROSE over the 4 candles before it,
                  +1 if it fell (this is how the original script labels; kept as is)
    filters       volatility (ATR(1) > ATR(10)) and regime (KLMF slope >= -0.1);
                  ADX filter off
    kernel        Nadaraya-Watson rational quadratic (lookback 8, weight 8, level 25):
                  a long needs the kernel rising, a short needs it falling
    entry         when the filtered signal changes direction and the kernel agrees
    exit          4 candles later (the model's horizon), or a stop/target added by us

Known differences from TradingView, all small:
    - every candle gets a prediction (TradingView only computes the last 2000 on
      the chart), so the neighbour memory starts filling earlier
    - indicators warm up from the first candle fed, so the first few hundred
      candles differ slightly from a chart with more history behind it
"""

from __future__ import annotations

import math
from collections import deque


# --- streaming building blocks (Pine semantics) ---------------------------------------

class EMA:
    """ta.ema: seeded with the simple average of the first n values."""

    def __init__(self, n: int):
        self.n, self.a, self.v, self.seed = n, 2 / (n + 1), None, []

    def update(self, x):
        if x is None:
            return self.v
        if self.v is None:
            self.seed.append(x)
            if len(self.seed) < self.n:
                return None
            self.v = sum(self.seed) / self.n
            return self.v
        self.v = self.a * x + (1 - self.a) * self.v
        return self.v


class RMA(EMA):
    """ta.rma (Wilder's average): like EMA with alpha = 1/n."""

    def __init__(self, n: int):
        super().__init__(n)
        self.a = 1 / n


class SMA:
    def __init__(self, n: int):
        self.n, self.q = n, deque(maxlen=n)

    def update(self, x):
        if x is None:
            return None
        self.q.append(x)
        return sum(self.q) / self.n if len(self.q) == self.n else None


class RSI:
    def __init__(self, n: int):
        self.up, self.dn, self.prev = RMA(n), RMA(n), None

    def update(self, x):
        if self.prev is None:
            self.prev = x
            return None
        ch, self.prev = x - self.prev, x
        u, d = self.up.update(max(ch, 0.0)), self.dn.update(max(-ch, 0.0))
        if u is None or d is None:
            return None
        if d == 0:
            return 100.0
        if u == 0:
            return 0.0
        return 100 - 100 / (1 + u / d)


class CCI:
    def __init__(self, n: int):
        self.n, self.q = n, deque(maxlen=n)

    def update(self, x):
        self.q.append(x)
        if len(self.q) < self.n:
            return None
        m = sum(self.q) / self.n
        dev = sum(abs(v - m) for v in self.q) / self.n
        return (x - m) / (0.015 * dev) if dev else 0.0


class Normalize:
    """ml.normalize: scale by the running min and max seen so far."""

    def __init__(self, lo=0.0, hi=1.0):
        self.lo, self.hi, self.mn, self.mx = lo, hi, 10e10, -10e10

    def update(self, x):
        if x is None:
            return None
        self.mn, self.mx = min(self.mn, x), max(self.mx, x)
        return self.lo + (self.hi - self.lo) * (x - self.mn) / max(self.mx - self.mn, 10e-10)


def rescale(x, old_lo, old_hi, lo=0.0, hi=1.0):
    if x is None:
        return None
    return lo + (hi - lo) * (x - old_lo) / max(old_hi - old_lo, 10e-10)


class NRSI:                                   # ml.n_rsi(close, n1, n2)
    def __init__(self, n1, n2):
        self.rsi, self.ema = RSI(n1), EMA(n2)

    def update(self, c, h, l, hlc3):
        return rescale(self.ema.update(self.rsi.update(c)), 0, 100)


class NCCI:                                   # ml.n_cci(close, n1, n2)
    def __init__(self, n1, n2):
        self.cci, self.ema, self.norm = CCI(n1), EMA(n2), Normalize()

    def update(self, c, h, l, hlc3):
        return self.norm.update(self.ema.update(self.cci.update(c)))


class NWT:                                    # ml.n_wt(hlc3, n1, n2): WaveTrend
    def __init__(self, n1, n2):
        self.e1, self.e2, self.e3, self.sma, self.norm = EMA(n1), EMA(n1), EMA(n2), SMA(4), Normalize()

    def update(self, c, h, l, hlc3):
        e1 = self.e1.update(hlc3)
        if e1 is None:
            return None
        e2 = self.e2.update(abs(hlc3 - e1))
        if not e2:
            return None
        wt1 = self.e3.update((hlc3 - e1) / (0.015 * e2))
        wt2 = self.sma.update(wt1)
        if wt1 is None or wt2 is None:
            return None
        return self.norm.update(wt1 - wt2)


class NADX:                                   # ml.n_adx(high, low, close, n1)
    def __init__(self, n):
        self.n, self.prev, self.tr_s, self.p_s, self.m_s, self.rma = n, None, 0.0, 0.0, 0.0, RMA(n)

    def update(self, c, h, l, hlc3):
        if self.prev is None:
            self.prev = (h, l, c)
            return None
        ph, pl, pc = self.prev
        self.prev = (h, l, c)
        tr = max(h - l, abs(h - pc), abs(l - pc))
        up, dn = h - ph, pl - l
        plus = max(up, 0.0) if up > dn else 0.0
        minus = max(dn, 0.0) if dn > up else 0.0
        n = self.n
        self.tr_s = self.tr_s - self.tr_s / n + tr
        self.p_s = self.p_s - self.p_s / n + plus
        self.m_s = self.m_s - self.m_s / n + minus
        if self.tr_s == 0:
            return None
        dip, dim = self.p_s / self.tr_s * 100, self.m_s / self.tr_s * 100
        if dip + dim == 0:
            return None
        return rescale(self.rma.update(abs(dip - dim) / (dip + dim) * 100), 0, 100)


class Regime:                                 # ml.regime_filter(ohlc4, threshold)
    def __init__(self, threshold=-0.1):
        self.th, self.v1, self.v2, self.klmf, self.src1, self.ema = threshold, 0.0, 0.0, 0.0, None, EMA(200)

    def update(self, o, h, l, c) -> bool:
        src = (o + h + l + c) / 4
        prev_src = self.src1 if self.src1 is not None else src
        self.src1 = src
        self.v1 = 0.2 * (src - prev_src) + 0.8 * self.v1
        self.v2 = 0.1 * (h - l) + 0.8 * self.v2
        if self.v2 == 0:
            return False
        omega = abs(self.v1 / self.v2)
        alpha = (-omega ** 2 + math.sqrt(omega ** 4 + 16 * omega ** 2)) / 8
        prev_k = self.klmf
        self.klmf = alpha * src + (1 - alpha) * self.klmf
        slope = abs(self.klmf - prev_k)
        avg = self.ema.update(slope)
        if not avg:
            return False                       # Pine: na until 200 values -> comparison false
        return (slope - avg) / avg >= self.th


def rational_quadratic(closes, lookback=8, weight=8.0, level=25):
    """kernels.rationalQuadratic: closes newest LAST; uses the latest level+2 values."""
    cw = tw = 0.0
    for i in range(level + 2):
        y = closes[-1 - i]
        w = (1 + (i ** 2 / (lookback ** 2 * 2 * weight))) ** -weight
        cw += y * w
        tw += w
    return cw / tw


# --- the classifier --------------------------------------------------------------------

class Lorentzian:
    def __init__(self, neighbours=8, max_bars_back=2000, regime_threshold=-0.1,
                 kernel_lookback=8, kernel_weight=8.0, kernel_level=25):
        self.k, self.max_back = neighbours, max_bars_back
        self.feats = [NRSI(14, 1), NWT(10, 11), NCCI(20, 1), NADX(20), NRSI(9, 1)]
        self.regime = Regime(regime_threshold)
        self.atr1, self.atr10, self.prev_c = RMA(1), RMA(10), None
        self.kl, self.kw, self.kx = kernel_lookback, kernel_weight, kernel_level
        self.X: list = []                     # stored feature vectors (None until warm)
        self.y: list = []                     # stored labels
        self.closes: deque = deque(maxlen=max(kernel_level + 3, 5))
        self.preds: list = []
        self.dists: list = []
        self.prediction = 0
        self.signal = 0
        self.yhat_prev = None
        self.n = 0

    def _label(self) -> int:
        if len(self.closes) < 5:
            return 0
        then, now = self.closes[-5], self.closes[-1]
        return -1 if then < now else 1 if then > now else 0

    def update(self, o: float, h: float, l: float, c: float) -> dict:
        """Feed one closed candle. Returns this candle's prediction and signals."""
        self.n += 1
        hlc3 = (h + l + c) / 3
        f = [ft.update(c, h, l, hlc3) for ft in self.feats]
        x = None if any(v is None for v in f) else tuple(f)
        tr = (h - l) if self.prev_c is None else max(h - l, abs(h - self.prev_c), abs(l - self.prev_c))
        self.prev_c = c
        a1, a10 = self.atr1.update(tr), self.atr10.update(tr)
        vol_ok = a1 is not None and a10 is not None and a1 > a10
        reg_ok = self.regime.update(o, h, l, c)
        self.closes.append(c)
        self.X.append(x)
        self.y.append(self._label())

        # approximate nearest neighbours (oldest stored candles first, as in the script)
        if x is not None:
            last = -1.0
            size = min(self.max_back - 1, len(self.y) - 1)
            X, y, k = self.X, self.y, self.k
            for i in range(size + 1):
                xi = X[i]
                if xi is None or i % 4 == 0:
                    continue
                d = (math.log1p(abs(x[0] - xi[0])) + math.log1p(abs(x[1] - xi[1])) +
                     math.log1p(abs(x[2] - xi[2])) + math.log1p(abs(x[3] - xi[3])) +
                     math.log1p(abs(x[4] - xi[4])))
                if d >= last:
                    last = d
                    self.dists.append(d)
                    self.preds.append(y[i])
                    if len(self.preds) > k:
                        last = self.dists[round(k * 3 / 4)]
                        self.dists.pop(0)
                        self.preds.pop(0)
            self.prediction = sum(self.preds)

        prev_signal = self.signal
        if vol_ok and reg_ok and self.prediction > 0:
            self.signal = 1
        elif vol_ok and reg_ok and self.prediction < 0:
            self.signal = -1
        changed = self.signal != prev_signal

        yhat = rational_quadratic(self.closes, self.kl, self.kw, self.kx) if len(self.closes) >= self.kx + 2 else None
        bull = yhat is not None and self.yhat_prev is not None and self.yhat_prev < yhat
        bear = yhat is not None and self.yhat_prev is not None and self.yhat_prev > yhat
        self.yhat_prev = yhat
        long_ = changed and self.signal == 1 and bull
        short = changed and self.signal == -1 and bear
        return {"prediction": self.prediction, "signal": self.signal, "start_long": long_,
                "start_short": short, "kernel": yhat, "volatility_ok": vol_ok, "regime_ok": reg_ok,
                "warm": x is not None}


class Cached:
    """Runs one Lorentzian model per price series, fed incrementally.

    Backtests ask about history[:t+1] for t = 0, 1, 2, ...; this feeds only the new
    candles each time (the model is causal, so the answer for candle t never changes)."""

    def __init__(self, key_fn, ohlc_fn, **settings):
        self.key_fn, self.ohlc_fn, self.settings = key_fn, ohlc_fn, settings
        self.model, self.keys, self.outs = None, [], []

    def at(self, history: list) -> dict | None:
        n = len(history)
        if n == 0:
            return None
        if self.keys and n <= len(self.keys) and self.keys[0] == self.key_fn(history[0]) \
                and self.keys[n - 1] == self.key_fn(history[-1]):
            return self.outs[n - 1]
        if not (self.keys and self.keys[0] == self.key_fn(history[0]) and n > len(self.keys)
                and self.keys[-1] == self.key_fn(history[len(self.keys) - 1])):
            self.model, self.keys, self.outs = Lorentzian(**self.settings), [], []
        for b in history[len(self.keys):]:
            self.outs.append(self.model.update(*self.ohlc_fn(b)))
            self.keys.append(self.key_fn(b))
        return self.outs[-1]
