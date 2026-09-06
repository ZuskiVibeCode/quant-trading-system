"""Base research agent — signals only, never places orders."""

from __future__ import annotations

from abc import ABC, abstractmethod

from quant_system.market_data.interface import MarketData
from quant_system.models.signal import Signal


class ResearchAgent(ABC):
    agent_id: str

    def __init__(self, market_data: MarketData):
        self.market_data = market_data

    @abstractmethod
    def analyze(self, ticker: str, *, benchmark: str = "SPY") -> Signal:
        """Emit one frozen Signal for ticker. Must not invent trades."""
