"""Numbers for honest testing: Sharpe, the Deflated Sharpe Ratio, drawdown, regime.

The Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014) answers: given how
many versions of this idea were tried, how likely is it that this Sharpe ratio
is real and not the luckiest of N attempts? It returns a probability, 0..1.
0.95 or more is the usual bar. Every extra variation raises the bar.
"""

from __future__ import annotations

import math
from statistics import NormalDist

_N = NormalDist()
EULER = 0.5772156649015329


def returns(curve: list[float]) -> list[float]:
    return [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve)) if curve[i - 1]]


def sharpe(rets: list[float]) -> float:
    """Per-period Sharpe ratio (not annualised)."""
    if len(rets) < 2:
        return 0.0
    m = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))
    return m / sd if sd > 0 else 0.0


def _moments(rets: list[float]) -> tuple[float, float]:
    """Skewness and (non-excess) kurtosis."""
    n = len(rets)
    if n < 3:
        return 0.0, 3.0
    m = sum(rets) / n
    var = sum((r - m) ** 2 for r in rets) / n
    if var <= 0:
        return 0.0, 3.0
    sd = math.sqrt(var)
    skew = sum(((r - m) / sd) ** 3 for r in rets) / n
    kurt = sum(((r - m) / sd) ** 4 for r in rets) / n
    return skew, kurt


def expected_max_sharpe(trials: int, sr_var: float) -> float:
    """The Sharpe the best of `trials` worthless strategies would show by luck."""
    if trials <= 1:
        return 0.0
    a = _N.inv_cdf(1 - 1 / trials)
    b = _N.inv_cdf(1 - 1 / (trials * math.e))
    return math.sqrt(sr_var) * ((1 - EULER) * a + EULER * b)


def deflated_sharpe(rets: list[float], trials: int, trial_sharpes: list[float] | None = None) -> float:
    """Probability (0..1) that the true Sharpe is above what luck alone explains.

    trials         how many versions of this idea were tested (from the ledger)
    trial_sharpes  their per-period Sharpe ratios, if recorded; otherwise the
                   variance of a Sharpe estimate under no edge, 1/(T-1), is used
    """
    t = len(rets)
    if t < 3:
        return 0.0
    sr = sharpe(rets)
    if trial_sharpes and len(trial_sharpes) >= 3:
        mu = sum(trial_sharpes) / len(trial_sharpes)
        var = sum((x - mu) ** 2 for x in trial_sharpes) / (len(trial_sharpes) - 1)
        var = max(var, 1 / (t - 1))
    else:
        var = 1 / (t - 1)
    sr0 = expected_max_sharpe(max(1, trials), var)
    skew, kurt = _moments(rets)
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if denom <= 0:
        return 0.0
    return _N.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom))


def max_drawdown(curve: list[float]) -> float:
    peak, mdd = curve[0] if curve else 0.0, 0.0
    for e in curve:
        peak = max(peak, e)
        if peak:
            mdd = max(mdd, (peak - e) / peak)
    return mdd


def regime(closes: list[float]) -> str:
    """trending up / trending down / choppy / high volatility, from daily closes.

    High volatility: recent daily moves are 1.5x their longer-run size.
    Trending: the 50-day average sloped clearly and price is on that side of it.
    Otherwise choppy.
    """
    if len(closes) < 30:
        return "unknown"
    rets = [abs(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]
    recent, longer = rets[-10:], rets[-min(len(rets), 120):]
    if sum(recent) / len(recent) > 1.5 * sum(longer) / len(longer):
        return "high volatility"
    n = min(50, len(closes) - 10)
    sma_now = sum(closes[-n:]) / n
    sma_then = sum(closes[-n - 10:-10]) / n
    slope = (sma_now / sma_then - 1) if sma_then else 0.0
    move = sum(longer) / len(longer)                      # typical daily move
    if slope > 2 * move and closes[-1] > sma_now:
        return "trending up"
    if slope < -2 * move and closes[-1] < sma_now:
        return "trending down"
    return "choppy"
