"""Per-stock risk indicators vs the benchmark. Each is causal: day t uses bars up to and including t.

Returns are daily close-to-close (``pct_change``); the benchmark is ``BenchClose``
(SPY's close on the ticker's own sessions). Volatilities are sample standard
deviations (ddof=1) of daily returns and are NOT annualized: multiply by
sqrt(252) for an annual figure.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from quant_system.lab.indicators.registry import indicator


def _returns(series: pd.Series) -> pd.Series:
    return series.pct_change(fill_method=None)


@indicator("beta", length=(60, 20, 252))
def beta(bars: pd.DataFrame, length: int) -> pd.Series:
    """Rolling beta to SPY: sample cov(stock, SPY daily returns) / sample var(SPY) over length bars."""
    ret, bench = _returns(bars["Close"]), _returns(bars["BenchClose"])
    return ret.rolling(length).cov(bench) / bench.rolling(length).var().replace(0.0, np.nan)


@indicator("realized_vol", length=(20, 5, 252))
def realized_vol(bars: pd.DataFrame, length: int) -> pd.Series:
    """Sample stdev of daily close-to-close returns over length bars (daily, not annualized)."""
    return _returns(bars["Close"]).rolling(length).std()


@indicator("vol_vs_spy", length=(60, 20, 252))
def vol_vs_spy(bars: pd.DataFrame, length: int) -> pd.Series:
    """The stock's realized vol over SPY's, same length bars (1.5 = 50% more volatile than SPY)."""
    spy = _returns(bars["BenchClose"]).rolling(length).std()
    return _returns(bars["Close"]).rolling(length).std() / spy.replace(0.0, np.nan)
