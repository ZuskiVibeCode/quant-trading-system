"""Universe breadth indicators. Each is causal: day t uses bars up to and including t.

``breadth_above_sma50`` reads ``Breadth50``, which the engine fills the same way
it fills ``BenchClose``: one series computed once from the already-loaded
universe's daily closes (share of names above their own 50-day SMA), then
aligned onto each ticker's sessions. The indicator itself does no cross-stock
work.
"""

from __future__ import annotations

import pandas as pd

from quant_system.lab.indicators.registry import indicator


@indicator("breadth_above_sma50")
def breadth_above_sma50(bars: pd.DataFrame) -> pd.Series:
    """Share of universe names whose close is above their own 50-day SMA (0-1).

    The SMA length is fixed at 50 (no setting): the engine precomputes one
    ``Breadth50`` column from the run's already-loaded daily bars. A name counts
    only on dates where it has a bar and at least 50 bars of its own history;
    the value is NaN when no name is eligible. Every ticker sees the same daily
    series on its own trading days.
    """
    return bars["Breadth50"].astype(float)


@indicator("breadth_above_sma200")
def breadth_above_sma200(bars: pd.DataFrame) -> pd.Series:
    """Share of universe names whose close is above their own 200-day SMA (0-1).

    The SMA length is fixed at 200 (no setting): the engine precomputes one
    ``Breadth200`` column over the full lab universe, like ``Breadth50``. A name
    counts only on dates where it has a bar and at least 200 bars of its own
    history; the value is NaN when no name is eligible.
    """
    return bars["Breadth200"].astype(float)


@indicator("net_new_highs")
def net_new_highs(bars: pd.DataFrame) -> pd.Series:
    """(New 252-day highs minus new 252-day lows) / eligible names, -1 to +1.

    The window is fixed at 252 (no setting): the engine precomputes one
    ``NetNewHighs252`` column over the full lab universe. Eligible: a bar that
    day and at least 252 bars of history. A new high (low) is a close equal to
    the max (min) of those 252 closes, including today. A flat name (max == min)
    counts as neither. NaN when no name is eligible.
    """
    return bars["NetNewHighs252"].astype(float)

