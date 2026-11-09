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
