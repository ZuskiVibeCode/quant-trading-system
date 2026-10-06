"""Lab benchmarks: SPY and equal-weight universe buy-and-hold, under the lab cost model.

Both benchmarks enter on the same day, the first fill any strategy could get in
the window: the signal day is the first window session on which the cost model
can price a trade in any name (20 bars of history; about session 20 of
discovery, session 1 of validation), and the fill is the next session's open
with each name's own slippage. Equal weight at entry, no rebalancing, marked at
each close, no exit cost. A name's liquidity bucket is its last reading on or
before the signal day, so a name that skipped that session still counts. Names
not tradable then or with no bar on the entry day are excluded and listed.
Before entry the benchmark holds cash. A name that stops trading mid-window is
carried at its last close (it holds its value like cash; the broad universe is
survivors only, so this is rare).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from quant_system.lab.costs import fill_price, slippage_panel
from quant_system.lab.data import bars_panel
from quant_system.lab.settings import LabCostSettings
from quant_system.lab.windows import ResolvedWindow


def _through(bars: dict[str, pd.DataFrame], end) -> dict[str, pd.DataFrame]:
    cut = pd.Timestamp(end)
    return {s: df.loc[df.index <= cut] for s, df in bars.items() if not df.empty}


def first_signal_day(slippage: pd.DataFrame, window: ResolvedWindow) -> pd.Timestamp | None:
    """First window session on which at least one name has a slippage bucket."""
    sessions = window.sessions(slippage.index)
    tradable = slippage.loc[sessions].notna().any(axis=1)
    hits = tradable[tradable]
    return hits.index[0] if len(hits) else None


def buy_and_hold(
    bars: dict[str, pd.DataFrame],
    tickers: list[str],
    window: ResolvedWindow,
    costs: LabCostSettings,
    *,
    signal_day: pd.Timestamp | None = None,
) -> dict[str, Any]:
    """Daily returns of an equal-weight buy-and-hold of ``tickers`` over the window."""
    wanted = [t.upper() for t in dict.fromkeys(tickers)]
    held = _through({t: bars[t] for t in wanted if t in bars}, window.end)
    sessions = window.sessions(bars_panel(_through(bars, window.end), "Close").index)
    flat = pd.Series(0.0, index=sessions)
    result: dict[str, Any] = {
        "tickers": [],
        "excluded": wanted,
        "signal_date": None,
        "entry_date": None,
        "returns": flat,
    }
    if not held or sessions.empty:
        return result
    slip = slippage_panel(held, costs)
    day = signal_day if signal_day is not None else first_signal_day(slip, window)
    later = sessions[sessions > day] if day is not None else sessions[:0]
    if later.empty:
        return result
    entry = later[0]
    open_ = bars_panel(held, "Open").reindex(sessions)
    close = bars_panel(held, "Close").reindex(sessions)
    bps = slip.ffill().loc[:day].iloc[-1]
    names = [t for t in held if pd.notna(bps.get(t)) and pd.notna(open_.at[entry, t])]
    if not names:
        return result
    entry_px = fill_price(open_.loc[entry, names], 1, bps[names])
    qty = (1.0 / len(names)) / entry_px
    marks = (close.loc[entry:, names].ffill() * qty).sum(axis=1)
    equity = pd.concat([pd.Series(1.0, index=sessions[sessions < entry]), marks])
    returns = (equity / equity.shift(1) - 1.0).fillna(equity.iloc[0] - 1.0)
    result.update(
        tickers=sorted(names),
        excluded=sorted(set(wanted) - set(names)),
        signal_date=str(day.date()),
        entry_date=str(entry.date()),
        returns=returns,
    )
    return result


def benchmark_returns(
    bars: dict[str, pd.DataFrame],
    universe: list[str],
    window: ResolvedWindow,
    costs: LabCostSettings,
    *,
    spy: str = "SPY",
) -> dict[str, dict[str, Any]]:
    """``{"SPY": ..., "equal_weight": ...}``, both entered on the same day."""
    names = [t.upper() for t in dict.fromkeys([spy, *universe])]
    pool = _through({t: bars[t] for t in names if t in bars}, window.end)
    day = first_signal_day(slippage_panel(pool, costs), window) if pool else None
    return {
        spy.upper(): buy_and_hold(bars, [spy], window, costs, signal_day=day),
        "equal_weight": buy_and_hold(bars, universe, window, costs, signal_day=day),
    }
