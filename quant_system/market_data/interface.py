"""MarketData interface — yfinance behind a thin abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


class MarketData(ABC):
    @abstractmethod
    def history(
        self,
        ticker: str,
        period: str = "1y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        """OHLCV history indexed by date. Columns: Open, High, Low, Close, Volume."""

    @abstractmethod
    def last_price(self, ticker: str) -> float:
        ...

    @abstractmethod
    def snapshot(self, ticker: str) -> dict[str, Any]:
        """Lightweight quote snapshot for decision logs."""
