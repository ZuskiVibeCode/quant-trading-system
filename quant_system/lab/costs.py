"""Lab fill and cost model.

A signal computed at day t's close fills at day t+1's open, never on the bar it
was computed from. If a name has no bar on t+1, there is no fill (the engine may
try again on the next signal). Slippage per side comes from the name's liquidity
bucket: the median dollar volume of the ``lookback_days`` sessions ending at t,
which is known at t's close. Alpaca charges no commission, so cost = slippage.

Dollar volume uses adjusted prices. Split adjustment cancels out (price / k,
volume * k); dividend adjustment shrinks older prices a little, so early-2016
dollar volume reads a few percent low. That only errs toward a costlier bucket.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from quant_system.lab.data import bars_panel
from quant_system.lab.settings import LabCostSettings


def median_dollar_volume(close: pd.DataFrame, volume: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """Median close x volume over the ``lookback`` bars ending each day (NaN until full)."""
    dv = close * volume
    return dv.apply(lambda s: s.dropna().rolling(lookback, min_periods=lookback).median()).reindex(
        close.index
    )


def slippage_bps(mdv: pd.DataFrame, costs: LabCostSettings) -> pd.DataFrame:
    """Per-side slippage in bps for each day/name; NaN = not tradable that day."""
    out = np.full(mdv.shape, np.nan)
    values = mdv.to_numpy(dtype=float)
    for bucket in sorted(costs.buckets, key=lambda b: b.min_dollar_volume):
        out[values >= bucket.min_dollar_volume] = bucket.bps
    return pd.DataFrame(out, index=mdv.index, columns=mdv.columns)


def slippage_panel(bars: dict[str, pd.DataFrame], costs: LabCostSettings) -> pd.DataFrame:
    """Slippage bps as of each signal day, for every name in ``bars``."""
    mdv = median_dollar_volume(
        bars_panel(bars, "Close"), bars_panel(bars, "Volume"), costs.lookback_days
    )
    return slippage_bps(mdv, costs)


def next_open(open_: pd.DataFrame) -> pd.DataFrame:
    """The fill price for a signal on each row's day: the next session's open.

    NaN when the name has no bar on the next session (or there is no next
    session), which means no fill, never a fallback to a same-day price.
    """
    return open_.shift(-1)


def fill_price(price, side: int, bps):
    """Price after slippage: buys (+1) pay up, sells (-1) receive less."""
    if side not in (1, -1):
        raise ValueError("side must be +1 (buy) or -1 (sell)")
    return price * (1.0 + side * bps / 10_000.0)


def trade_cost(traded_weight: pd.DataFrame, bps: pd.DataFrame) -> pd.Series:
    """Daily cost as a fraction of equity: sum of |weight traded| x slippage per name.

    Both frames are indexed by fill day: shift signal-day slippage forward one
    session first. Raises if any weight trades in a name with no slippage (not
    tradable on its signal day).
    """
    traded = traded_weight.abs()
    bps = bps.reindex(index=traded.index, columns=traded.columns)
    if ((traded > 0) & bps.isna()).any().any():
        raise ValueError("trade in a name that is not tradable that day (no slippage bucket)")
    return (traded * bps.fillna(0.0) / 10_000.0).sum(axis=1)
