"""Buy-and-hold benchmark curves for backtest results (SPY, equal-weight universe)."""

from __future__ import annotations

from typing import Any

import pandas as pd

BENCHMARK_DOC = (
    "buy-and-hold, equal weight at entry, no rebalancing, marked at each close; "
    "entry uses the sleeve fill model (next_open: open of the 2nd trading day, the "
    "first fill a day-1 signal can get; close: 1st day close) with the same "
    "slippage_bps; names with no bar on the entry day are excluded"
)


def _bar_frame(bars: dict[str, pd.DataFrame], ticker: str) -> pd.DataFrame | None:
    df = bars.get(ticker.upper())
    if df is None or df.empty:
        return None
    out = df.rename(columns=str.title)
    idx = pd.to_datetime(out.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    out = out.set_axis(idx.normalize())
    return out[~out.index.duplicated(keep="last")].sort_index()


def buy_and_hold_curve(
    bars: dict[str, pd.DataFrame],
    tickers: list[str],
    days: list[pd.Timestamp],
    *,
    cash: float,
    slippage_bps: float,
    fill_on: str,
) -> dict[str, Any]:
    """Equal-weight buy-and-hold of ``tickers`` over ``days``.

    Returns ``equity_curve`` with one mark per day, prefixed by starting cash so it
    lines up with sleeve curves in ``metrics.sleeve_summary``.
    """
    curve_days = [pd.Timestamp(d).normalize() for d in days]
    flat = {
        "tickers": [],
        "excluded": [t.upper() for t in tickers],
        "entry_date": None,
        "equity_curve": [float(cash)] * (len(curve_days) + 1),
    }
    entry_idx = 1 if fill_on == "next_open" else 0
    if len(curve_days) <= entry_idx:
        return flat
    entry_day = curve_days[entry_idx]
    price_col = "Open" if fill_on == "next_open" else "Close"

    frames: dict[str, pd.DataFrame] = {}
    for t in tickers:
        df = _bar_frame(bars, t)
        if df is not None and entry_day in df.index:
            frames[t.upper()] = df
    if not frames:
        return flat

    slip = float(slippage_bps) / 10_000.0
    per_name = float(cash) / len(frames)
    marks = pd.Series(0.0, index=pd.DatetimeIndex(curve_days[entry_idx:]))
    for t, df in frames.items():
        entry_px = float(df.loc[entry_day, price_col]) * (1.0 + slip)
        qty = per_name / entry_px
        closes = df["Close"].reindex(marks.index).ffill()
        marks = marks.add(closes * qty, fill_value=0.0)

    # Days before entry hold cash.
    curve = [float(cash)] * (entry_idx + 1) + [float(v) for v in marks.values]
    return {
        "tickers": sorted(frames),
        "excluded": sorted({t.upper() for t in tickers} - set(frames)),
        "entry_date": str(entry_day.date()),
        "equity_curve": curve,
    }
