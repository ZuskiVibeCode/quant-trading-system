"""yfinance-backed MarketData implementation."""

from __future__ import annotations

from typing import Any

import pandas as pd

from quant_system.market_data.interface import MarketData


class YFinanceMarketData(MarketData):
    def __init__(self):
        import yfinance as yf

        self._yf = yf
        self._cache: dict[tuple[str, str, str], pd.DataFrame] = {}

    def history(
        self,
        ticker: str,
        period: str = "1y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        key = (ticker.upper(), period, interval)
        if key in self._cache:
            return self._cache[key].copy()
        t = self._yf.Ticker(ticker)
        df = t.history(period=period, interval=interval, auto_adjust=True)
        if df is None or df.empty:
            raise ValueError(f"No history for {ticker}")
        # Normalize columns
        df = df.rename(columns=str.title)
        needed = ["Open", "High", "Low", "Close", "Volume"]
        for c in needed:
            if c not in df.columns:
                raise ValueError(f"Missing column {c} for {ticker}")
        out = df[needed].copy()
        self._cache[key] = out
        return out.copy()

    def last_price(self, ticker: str) -> float:
        df = self.history(ticker, period="5d", interval="1d")
        return float(df["Close"].iloc[-1])

    def snapshot(self, ticker: str) -> dict[str, Any]:
        df = self.history(ticker, period="5d", interval="1d")
        last = df.iloc[-1]
        return {
            "ticker": ticker.upper(),
            "last_close": float(last["Close"]),
            "last_volume": float(last["Volume"]),
            "asof": str(df.index[-1]),
            "bars": len(df),
        }


class MockMarketData(MarketData):
    """Deterministic prices for tests / offline dry-run."""

    def __init__(self, prices: dict[str, float] | None = None, bars: int = 250):
        self.prices = prices or {}
        self.bars = bars

    def _series(self, ticker: str) -> pd.DataFrame:
        import numpy as np

        base = float(self.prices.get(ticker.upper(), 50.0))
        rng = np.random.default_rng(abs(hash(ticker.upper())) % (2**32))
        rets = rng.normal(0.0005, 0.02, size=self.bars)
        closes = base * np.cumprod(1 + rets)
        # Ensure last close equals configured price if provided
        if ticker.upper() in self.prices:
            closes = closes * (base / closes[-1])
        idx = pd.date_range(end=pd.Timestamp.now(tz="UTC").tz_localize(None).normalize(), periods=self.bars, freq="B")
        high = closes * 1.01
        low = closes * 0.99
        open_ = closes * (1 + rng.normal(0, 0.002, size=self.bars))
        vol = rng.integers(100_000, 1_000_000, size=self.bars)
        return pd.DataFrame(
            {"Open": open_, "High": high, "Low": low, "Close": closes, "Volume": vol},
            index=idx,
        )

    def history(self, ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        return self._series(ticker)

    def last_price(self, ticker: str) -> float:
        return float(self.history(ticker)["Close"].iloc[-1])

    def snapshot(self, ticker: str) -> dict[str, Any]:
        df = self.history(ticker)
        last = df.iloc[-1]
        return {
            "ticker": ticker.upper(),
            "last_close": float(last["Close"]),
            "last_volume": float(last["Volume"]),
            "asof": str(df.index[-1]),
            "bars": len(df),
            "mock": True,
        }
