"""Benchmark (SPY) regime indicators. Each is causal: day t uses bars up to and including t.

They read ``BenchClose``, the benchmark's close on the ticker's own sessions (the
engine fills it from SPY), so every name in a run sees the same market regime.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from quant_system.lab.indicators.registry import indicator


@indicator("spy_trend_dist", length=(200, 50, 300))
def spy_trend_dist(bars: pd.DataFrame, length: int) -> pd.Series:
    """SPY close vs its length-day SMA (0.05 = 5% above the trend line)."""
    bench = bars["BenchClose"]
    return bench / bench.rolling(length).mean() - 1.0


@indicator("spy_drawdown", length=(252, 60, 504))
def spy_drawdown(bars: pd.DataFrame, length: int) -> pd.Series:
    """SPY close vs its highest close of the last length days, 0 to -1 (-0.1 = 10% off the high)."""
    bench = bars["BenchClose"]
    return bench / bench.rolling(length).max() - 1.0


@indicator("spy_vol_ratio", fast=(10, 5, 20), slow=(60, 30, 250))
def spy_vol_ratio(bars: pd.DataFrame, fast: int, slow: int) -> pd.Series:
    """Stdev of SPY daily returns over fast days / over slow days (1.5 = recent vol 50% above normal).

    fast max (20) sits below slow min (30), so fast < slow at every setting.
    """
    ret = bars["BenchClose"].pct_change(fill_method=None)
    slow_sd = ret.rolling(slow).std()
    return ret.rolling(fast).std() / slow_sd.replace(0.0, np.nan)
