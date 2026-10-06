"""Lab metrics on daily return series: performance, vs-benchmark, deflated Sharpe, pass bar.

Sharpe is annualized from daily returns with risk-free = 0. Deflated Sharpe
follows Bailey and Lopez de Prado (2014): the probability that the true Sharpe
beats the best Sharpe expected from ``n_trials`` unskilled tries, given the
spread of the trial Sharpes and the skew and fat tails of the returns.
"""

from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252
EULER_MASCHERONI = 0.5772156649015329
DEFLATED_SHARPE_PASS = 0.95
_N = NormalDist()


def _clean(returns: pd.Series) -> pd.Series:
    return pd.Series(returns, dtype=float).dropna()


def cagr(returns: pd.Series) -> float:
    r = _clean(returns)
    if r.empty:
        return 0.0
    growth = float((1.0 + r).prod())
    if growth <= 0:
        return -1.0
    return growth ** (TRADING_DAYS_PER_YEAR / len(r)) - 1.0


def sharpe(returns: pd.Series) -> float:
    """Annualized Sharpe (risk-free = 0); 0.0 when undefined."""
    r = _clean(returns)
    if len(r) < 2:
        return 0.0
    std = float(r.std(ddof=1))
    if not std > 1e-12:  # constant series: float noise, not risk
        return 0.0
    return float(r.mean()) / std * math.sqrt(TRADING_DAYS_PER_YEAR)


def max_drawdown(returns: pd.Series) -> float:
    """Max peak-to-trough drop of the compounded curve, as a fraction (0..1)."""
    r = _clean(returns)
    if r.empty:
        return 0.0
    curve = np.concatenate([[1.0], (1.0 + r).cumprod().to_numpy()])
    peak = np.maximum.accumulate(curve)
    return float(np.max(1.0 - curve / peak))


def turnover(weights: pd.DataFrame) -> float:
    """Annualized one-way turnover: half the summed |weight change| per day, x 252.

    ``weights`` holds end-of-day weights (dates x names); the first row is
    traded from cash.
    """
    if weights.empty:
        return 0.0
    w = weights.fillna(0.0)
    traded = w.diff().fillna(w).abs().sum(axis=1)
    return float(traded.mean() * 0.5 * TRADING_DAYS_PER_YEAR)


def exposure(weights: pd.DataFrame) -> float:
    """Average gross weight invested (0 = all cash, 1 = fully invested)."""
    if weights.empty:
        return 0.0
    return float(weights.fillna(0.0).abs().sum(axis=1).mean())


def summary(
    returns: pd.Series,
    *,
    weights: pd.DataFrame | None = None,
    trade_count: int | None = None,
) -> dict[str, Any]:
    r = _clean(returns)
    out: dict[str, Any] = {
        "start": str(r.index[0].date()) if len(r) else None,
        "end": str(r.index[-1].date()) if len(r) else None,
        "days": int(len(r)),
        "total_return": float((1.0 + r).prod() - 1.0) if len(r) else 0.0,
        "cagr": cagr(r),
        "sharpe": sharpe(r),
        "max_drawdown": max_drawdown(r),
    }
    if weights is not None:
        out["turnover"] = turnover(weights)
        out["exposure"] = exposure(weights)
    if trade_count is not None:
        out["trade_count"] = int(trade_count)
    return out


def beta(returns: pd.Series, benchmark: pd.Series) -> float:
    r, b = _clean(returns).align(_clean(benchmark), join="inner")
    if len(r) < 2:
        return 0.0
    var = float(b.var(ddof=1))
    return float(r.cov(b) / var) if var > 0 else 0.0


def information_ratio(returns: pd.Series, benchmark: pd.Series) -> float:
    """Annualized mean / std of daily active return."""
    r, b = _clean(returns).align(_clean(benchmark), join="inner")
    return sharpe(r - b)


def compare(returns: pd.Series, benchmarks: dict[str, pd.Series]) -> dict[str, Any]:
    """Strategy vs each benchmark over the same days.

    ``beats_benchmarks`` is the gauntlet pass bar (same rule as agent replay):
    Sharpe above every benchmark's and positive excess CAGR against every one.
    """
    s_cagr, s_sharpe = cagr(returns), sharpe(returns)
    vs: dict[str, dict[str, float]] = {}
    for name, b in benchmarks.items():
        vs[name] = {
            "cagr": cagr(b),
            "sharpe": sharpe(b),
            "max_drawdown": max_drawdown(b),
            "excess_cagr": s_cagr - cagr(b),
            "sharpe_diff": s_sharpe - sharpe(b),
            "beta": beta(returns, b),
            "information_ratio": information_ratio(returns, b),
        }
    beats = bool(vs) and all(v["sharpe_diff"] > 0 and v["excess_cagr"] > 0 for v in vs.values())
    return {"vs_benchmarks": vs, "beats_benchmarks": beats}


def expected_max_sharpe(n_trials: int, sharpe_variance: float) -> float:
    """Best Sharpe expected from ``n_trials`` tries with zero true skill.

    Same units as ``sharpe_variance`` (variance of the trial Sharpes). 0 for a
    single trial: with nothing to choose from there is no selection bias.
    """
    if n_trials < 2 or sharpe_variance <= 0:
        return 0.0
    g = EULER_MASCHERONI
    z1 = _N.inv_cdf(1.0 - 1.0 / n_trials)
    z2 = _N.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(sharpe_variance) * ((1.0 - g) * z1 + g * z2)


def probabilistic_sharpe_from_stats(
    sharpe_per_period: float,
    benchmark_per_period: float,
    n_obs: int,
    skew: float,
    kurtosis: float,
) -> float:
    """P(true Sharpe > benchmark), all Sharpes per period; ``kurtosis`` is not excess."""
    if n_obs < 2:
        return 0.0
    denom = 1.0 - skew * sharpe_per_period + (kurtosis - 1.0) / 4.0 * sharpe_per_period**2
    z = (sharpe_per_period - benchmark_per_period) * math.sqrt(n_obs - 1) / math.sqrt(max(denom, 1e-12))
    return _N.cdf(z)


def deflated_sharpe_from_stats(
    sharpe_annual: float,
    n_obs: int,
    skew: float,
    kurtosis: float,
    n_trials: int,
    sharpe_variance_annual: float,
    *,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """Deflated Sharpe from annualized inputs (converted to per-period internally)."""
    scale = math.sqrt(periods_per_year)
    sr0 = expected_max_sharpe(n_trials, sharpe_variance_annual / periods_per_year)
    return probabilistic_sharpe_from_stats(sharpe_annual / scale, sr0, n_obs, skew, kurtosis)


def _moments(r: pd.Series) -> tuple[float, float]:
    """Population skew and (non-excess) kurtosis, which keep the PSR denominator >= 0."""
    x = r.to_numpy(dtype=float)
    d = x - x.mean()
    m2 = float(np.mean(d**2))
    if m2 <= 0:
        return 0.0, 3.0
    return float(np.mean(d**3)) / m2**1.5, float(np.mean(d**4)) / m2**2


def deflated_sharpe(returns: pd.Series, n_trials: int, sharpe_variance: float) -> float:
    """Deflated Sharpe of a daily return series.

    ``n_trials`` and ``sharpe_variance`` (variance of annualized trial Sharpes)
    come from ``trials.trial_stats("discovery")``, frozen at nomination.
    """
    r = _clean(returns)
    if len(r) < 2:
        return 0.0
    skew, kurt = _moments(r)
    return deflated_sharpe_from_stats(sharpe(r), len(r), skew, kurt, n_trials, sharpe_variance)


def pass_bar(
    window: str,
    comparison: dict[str, Any],
    deflated: float | None = None,
    *,
    required: tuple[str, ...] = ("SPY", "equal_weight"),
) -> dict[str, Any]:
    """Gauntlet verdict: beat SPY and equal-weight on Sharpe and CAGR, after costs.

    Validation and holdout also need deflated Sharpe above 0.95.
    """
    present = set(comparison.get("vs_benchmarks") or {})
    checks = {
        "beats_benchmarks": bool(comparison.get("beats_benchmarks")) and set(required) <= present
    }
    if window in ("validation", "holdout"):
        checks["deflated_sharpe"] = deflated is not None and deflated > DEFLATED_SHARPE_PASS
    return {"passed": all(checks.values()), "checks": checks}
