"""Starter indicator set. Each is causal: day t uses bars up to and including t."""

from __future__ import annotations

import numpy as np
import pandas as pd

from quant_system.lab.indicators.registry import indicator


def _wilder(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


@indicator("sma", length=(20, 2, 400))
def sma(bars: pd.DataFrame, length: int) -> pd.Series:
    """Simple moving average of close."""
    return bars["Close"].rolling(length).mean()


@indicator("ema", length=(20, 2, 400))
def ema(bars: pd.DataFrame, length: int) -> pd.Series:
    """Exponential moving average of close (span = length)."""
    return bars["Close"].ewm(span=length, adjust=False, min_periods=length).mean()


@indicator("rsi", length=(14, 2, 100))
def rsi(bars: pd.DataFrame, length: int) -> pd.Series:
    """Wilder RSI of close, 0-100."""
    delta = bars["Close"].diff()
    gain = _wilder(delta.clip(lower=0.0), length)
    loss = _wilder(-delta.clip(upper=0.0), length)
    out = 100.0 - 100.0 / (1.0 + gain / loss)
    return out.mask((loss == 0) & gain.notna(), 100.0)


def _macd_parts(close: pd.Series, fast: int, slow: int, signal: int) -> tuple[pd.Series, pd.Series]:
    line = (
        close.ewm(span=fast, adjust=False, min_periods=fast).mean()
        - close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    )
    return line, line.ewm(span=signal, adjust=False, min_periods=signal).mean()


@indicator("macd", fast=(12, 2, 100), slow=(26, 3, 200), signal=(9, 2, 50))
def macd(bars: pd.DataFrame, fast: int, slow: int, signal: int) -> pd.Series:
    """MACD line: fast EMA minus slow EMA of close."""
    return _macd_parts(bars["Close"], fast, slow, signal)[0]


@indicator("macd_hist", fast=(12, 2, 100), slow=(26, 3, 200), signal=(9, 2, 50))
def macd_hist(bars: pd.DataFrame, fast: int, slow: int, signal: int) -> pd.Series:
    """MACD histogram: MACD line minus its signal EMA (crosses 0 on line/signal crosses)."""
    line, sig = _macd_parts(bars["Close"], fast, slow, signal)
    return line - sig


def _true_range(bars: pd.DataFrame) -> pd.Series:
    prev = bars["Close"].shift(1)
    return pd.concat(
        [bars["High"] - bars["Low"], (bars["High"] - prev).abs(), (bars["Low"] - prev).abs()], axis=1
    ).max(axis=1, skipna=False).fillna(bars["High"] - bars["Low"])


@indicator("atr", length=(14, 2, 100))
def atr(bars: pd.DataFrame, length: int) -> pd.Series:
    """Wilder average true range, in price units."""
    return _wilder(_true_range(bars), length)


@indicator("atr_pct", length=(14, 2, 100))
def atr_pct(bars: pd.DataFrame, length: int) -> pd.Series:
    """ATR as a fraction of close (comparable across prices)."""
    return _wilder(_true_range(bars), length) / bars["Close"]


@indicator("bb_pctb", length=(20, 2, 400), k=(2.0, 0.5, 4.0))
def bb_pctb(bars: pd.DataFrame, length: int, k: float) -> pd.Series:
    """Bollinger %B: 0 at the lower band, 1 at the upper band."""
    close = bars["Close"]
    mid = close.rolling(length).mean()
    width = 2.0 * k * close.rolling(length).std(ddof=0)
    return (close - (mid - width / 2.0)) / width.replace(0.0, np.nan)


@indicator("roc", length=(20, 1, 400))
def roc(bars: pd.DataFrame, length: int) -> pd.Series:
    """Rate of change of close over length bars (0.05 = +5%)."""
    close = bars["Close"]
    return close / close.shift(length) - 1.0


@indicator("zscore", length=(20, 2, 400))
def zscore(bars: pd.DataFrame, length: int) -> pd.Series:
    """Close minus its rolling mean, in rolling standard deviations."""
    close = bars["Close"]
    std = close.rolling(length).std(ddof=1)
    return (close - close.rolling(length).mean()) / std.replace(0.0, np.nan)


@indicator("rel_strength", length=(60, 1, 400))
def rel_strength(bars: pd.DataFrame, length: int) -> pd.Series:
    """Return over length bars relative to the benchmark (SPY): 0 = kept pace."""
    close, bench = bars["Close"], bars["BenchClose"]
    return (close / close.shift(length)) / (bench / bench.shift(length)) - 1.0


@indicator("rel_volume", length=(20, 2, 400))
def rel_volume(bars: pd.DataFrame, length: int) -> pd.Series:
    """Today's volume over its rolling mean (1 = normal)."""
    vol = bars["Volume"]
    return vol / vol.rolling(length).mean().replace(0.0, np.nan)


@indicator("donchian_high", length=(20, 2, 400))
def donchian_high(bars: pd.DataFrame, length: int) -> pd.Series:
    """Highest high of the previous length bars (excludes today, for breakouts)."""
    return bars["High"].rolling(length).max().shift(1)


@indicator("donchian_low", length=(20, 2, 400))
def donchian_low(bars: pd.DataFrame, length: int) -> pd.Series:
    """Lowest low of the previous length bars (excludes today, for breakdowns)."""
    return bars["Low"].rolling(length).min().shift(1)
