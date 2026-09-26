"""As-of MarketData adapter — truncates cached OHLCV to the replay date (no lookahead)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from quant_system.market_data.interface import MarketData

# Approximate calendar-day spans for yfinance-style period strings.
_PERIOD_CALENDAR_DAYS: dict[str, int | None] = {
    "1d": 1,
    "5d": 10,
    "1wk": 7,
    "1mo": 35,
    "3mo": 100,
    "6mo": 200,
    "1y": 400,
    "2y": 800,
    "5y": 1900,
    "10y": 3700,
    "ytd": None,  # special
    "max": None,
}


def _normalize_ts(value: str | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if getattr(ts, "tzinfo", None) is not None or getattr(ts, "tz", None) is not None:
        ts = ts.tz_localize(None) if ts.tzinfo else ts
        if getattr(ts, "tz", None) is not None:
            ts = ts.tz_localize(None)
    return ts.normalize()


class AsOfMarketData(MarketData):
    """MarketData over a preloaded bar dict; history/last_price never see future bars."""

    def __init__(
        self,
        bars: dict[str, pd.DataFrame],
        *,
        as_of: str | pd.Timestamp | None = None,
    ):
        self._bars: dict[str, pd.DataFrame] = {}
        for k, df in (bars or {}).items():
            if df is None or getattr(df, "empty", True):
                continue
            norm = df.copy()
            if not isinstance(norm.index, pd.DatetimeIndex):
                norm.index = pd.to_datetime(norm.index)
            if getattr(norm.index, "tz", None) is not None:
                norm.index = norm.index.tz_localize(None)
            norm.index = norm.index.normalize()
            norm = norm.rename(columns=str.title)
            needed = ["Open", "High", "Low", "Close", "Volume"]
            for c in needed:
                if c not in norm.columns:
                    raise ValueError(f"{k}: missing column {c}")
            self._bars[k.upper()] = norm[needed].sort_index()
        self._as_of: pd.Timestamp | None = _normalize_ts(as_of) if as_of is not None else None

    @property
    def as_of(self) -> pd.Timestamp | None:
        return self._as_of

    def set_as_of(self, as_of: str | pd.Timestamp) -> None:
        self._as_of = _normalize_ts(as_of)

    def tickers(self) -> list[str]:
        return sorted(self._bars.keys())

    def trading_days(
        self,
        start: str | pd.Timestamp,
        end: str | pd.Timestamp,
        *,
        reference: str = "SPY",
    ) -> list[pd.Timestamp]:
        """Business days present in reference (fallback: union of all tickers)."""
        start_ts = _normalize_ts(start)
        end_ts = _normalize_ts(end)
        ref = self._bars.get(reference.upper())
        if ref is None or ref.empty:
            if not self._bars:
                return []
            idx = None
            for df in self._bars.values():
                idx = df.index if idx is None else idx.union(df.index)
            dates = idx.sort_values()  # type: ignore[union-attr]
        else:
            dates = ref.index.sort_values()
        mask = (dates >= start_ts) & (dates <= end_ts)
        return [pd.Timestamp(d).normalize() for d in dates[mask]]

    def _asof_frame(self, ticker: str) -> pd.DataFrame:
        key = ticker.upper()
        if key not in self._bars:
            raise ValueError(f"No cached bars for {ticker}")
        df = self._bars[key]
        if self._as_of is None:
            return df.copy()
        truncated = df.loc[df.index <= self._as_of]
        if truncated.empty:
            raise ValueError(f"No bars for {ticker} on or before {self._as_of.date()}")
        return truncated.copy()

    def _apply_period(self, df: pd.DataFrame, period: str) -> pd.DataFrame:
        if df.empty:
            return df
        p = (period or "1y").lower().strip()
        if p == "max":
            return df
        end = df.index[-1]
        if p == "ytd":
            start = pd.Timestamp(year=int(end.year), month=1, day=1)
            return df.loc[df.index >= start]
        days = _PERIOD_CALENDAR_DAYS.get(p)
        if days is None:
            # Unknown period → return all available as-of history
            return df
        start = end - pd.Timedelta(days=int(days))
        return df.loc[df.index >= start]

    def history(
        self,
        ticker: str,
        period: str = "1y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        if interval not in ("1d", "1D", "d", "day"):
            # v1 daily only
            raise ValueError(f"AsOfMarketData only supports daily bars (got interval={interval})")
        df = self._asof_frame(ticker)
        out = self._apply_period(df, period)
        if out.empty:
            raise ValueError(f"No history for {ticker} period={period} as_of={self._as_of}")
        return out.copy()

    def last_price(self, ticker: str) -> float:
        df = self._asof_frame(ticker)
        return float(df["Close"].iloc[-1])

    def open_price(self, ticker: str) -> float:
        """As-of session open (for next-bar-open fill variants)."""
        df = self._asof_frame(ticker)
        return float(df["Open"].iloc[-1])

    def next_open(self, ticker: str) -> float | None:
        """Open of the bar immediately after as_of, if present in cache."""
        key = ticker.upper()
        if key not in self._bars or self._as_of is None:
            return None
        df = self._bars[key]
        future = df.loc[df.index > self._as_of]
        if future.empty:
            return None
        return float(future["Open"].iloc[0])

    def snapshot(self, ticker: str) -> dict[str, Any]:
        df = self._asof_frame(ticker)
        last = df.iloc[-1]
        return {
            "ticker": ticker.upper(),
            "last_close": float(last["Close"]),
            "last_volume": float(last["Volume"]),
            "asof": str(df.index[-1].date()),
            "bars": len(df),
            "backtest": True,
        }
