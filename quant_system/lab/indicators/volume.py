"""Volume indicators. Each is causal: day t uses bars up to and including t.

Daily-bar versions of the common charting volume studies (OBV, accumulation /
distribution, Chaikin money flow and oscillator, money flow index, VWAP,
anchored VWAP, volume oscillator). Running totals like OBV and the A/D line
depend on where the history starts and on how much a stock trades, so each one
here is a windowed or normalized form: values compare across stocks and don't
drift with the data's start date. VWAP is over whole days (no intraday bars).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from quant_system.lab.indicators.registry import indicator


def _typical(bars: pd.DataFrame) -> pd.Series:
    return (bars["High"] + bars["Low"] + bars["Close"]) / 3.0


def _ratio(num: pd.Series, den: pd.Series) -> pd.Series:
    return num / den.replace(0.0, np.nan)


def _money_flow_multiplier(bars: pd.DataFrame) -> pd.Series:
    """+1 = closed at the high, -1 = at the low, 0 = mid-range (or no range)."""
    high, low, close = bars["High"], bars["Low"], bars["Close"]
    rng = high - low
    return (((close - low) - (high - close)) / rng.replace(0.0, np.nan)).where(rng != 0, 0.0)


@indicator("obv_trend", length=(20, 2, 400))
def obv_trend(bars: pd.DataFrame, length: int) -> pd.Series:
    """On-balance volume over length bars as a share of all volume: +1 = every bar up, -1 = every bar down."""
    vol = bars["Volume"]
    signed = np.sign(bars["Close"].diff()) * vol
    return _ratio(signed.rolling(length).sum(), vol.where(signed.notna()).rolling(length).sum())


@indicator("cmf", length=(20, 2, 400))
def cmf(bars: pd.DataFrame, length: int) -> pd.Series:
    """Chaikin money flow, -1 to +1: volume-weighted closing position within each day's range."""
    vol = bars["Volume"]
    return _ratio((_money_flow_multiplier(bars) * vol).rolling(length).sum(), vol.rolling(length).sum())


@indicator("chaikin_osc", fast=(3, 2, 50), slow=(10, 3, 200))
def chaikin_osc(bars: pd.DataFrame, fast: int, slow: int) -> pd.Series:
    """Chaikin oscillator: fast EMA minus slow EMA of the A/D line, in units of average daily volume."""
    vol = bars["Volume"]
    ad = (_money_flow_multiplier(bars) * vol).cumsum()
    # EMAs pass a constant offset through, so the start of the history cancels out.
    diff = ad.ewm(span=fast, adjust=False, min_periods=fast).mean() - ad.ewm(
        span=slow, adjust=False, min_periods=slow
    ).mean()
    return _ratio(diff, vol.rolling(slow).mean())


@indicator("mfi", length=(14, 2, 100))
def mfi(bars: pd.DataFrame, length: int) -> pd.Series:
    """Money flow index, 0-100: a volume-weighted RSI of the typical price."""
    tp = _typical(bars)
    flow = tp * bars["Volume"]
    change = tp.diff()
    pos = flow.where(change > 0, 0.0).where(change.notna())
    neg = flow.where(change < 0, 0.0).where(change.notna())
    p, n = pos.rolling(length).sum(), neg.rolling(length).sum()
    return 100.0 * _ratio(p, p + n)


@indicator("vwap_dist", length=(20, 2, 400))
def vwap_dist(bars: pd.DataFrame, length: int) -> pd.Series:
    """Close vs the rolling length-day VWAP of typical price (0.02 = 2% above)."""
    vol = bars["Volume"]
    vwap = _ratio((_typical(bars) * vol).rolling(length).sum(), vol.rolling(length).sum())
    return bars["Close"] / vwap - 1.0


def _anchored_vwap_dist(bars: pd.DataFrame, length: int, column: str, highest: bool) -> pd.Series:
    """Close vs the VWAP anchored at the latest highest (or lowest) bar of the last length bars."""
    n = len(bars)
    out = np.full(n, np.nan)
    if n >= length:
        vol = bars["Volume"].fillna(0.0).to_numpy(dtype=float)
        pv = (_typical(bars) * bars["Volume"]).fillna(0.0).to_numpy(dtype=float)
        newest_first = sliding_window_view(bars[column].to_numpy(dtype=float), length)[:, ::-1]
        back = newest_first.argmax(axis=1) if highest else newest_first.argmin(axis=1)
        t = np.arange(length - 1, n)
        anchor = t - back
        cpv = np.concatenate([[0.0], np.cumsum(pv)])
        cv = np.concatenate([[0.0], np.cumsum(vol)])
        den = cv[t + 1] - cv[anchor]
        with np.errstate(divide="ignore", invalid="ignore"):
            out[t] = np.where(den > 0, (cpv[t + 1] - cpv[anchor]) / den, np.nan)
    return bars["Close"] / pd.Series(out, index=bars.index) - 1.0


@indicator("avwap_high_dist", length=(60, 5, 400))
def avwap_high_dist(bars: pd.DataFrame, length: int) -> pd.Series:
    """Close vs the VWAP anchored at the highest high of the last length bars (0.02 = 2% above)."""
    return _anchored_vwap_dist(bars, length, "High", highest=True)


@indicator("avwap_low_dist", length=(60, 5, 400))
def avwap_low_dist(bars: pd.DataFrame, length: int) -> pd.Series:
    """Close vs the VWAP anchored at the lowest low of the last length bars (-0.02 = 2% below)."""
    return _anchored_vwap_dist(bars, length, "Low", highest=False)


@indicator("volume_osc", fast=(5, 1, 100), slow=(20, 2, 400))
def volume_osc(bars: pd.DataFrame, fast: int, slow: int) -> pd.Series:
    """Volume oscillator: fast over slow average volume, minus 1 (0.5 = volume running 50% above normal)."""
    vol = bars["Volume"]
    return _ratio(vol.rolling(fast).mean(), vol.rolling(slow).mean()) - 1.0
