"""FundamentalAgent — stub with TODOs (signals only)."""

from __future__ import annotations

from quant_system.market_data.interface import MarketData
from quant_system.models.signal import Direction, Signal
from quant_system.research.base import ResearchAgent


class FundamentalAgent(ResearchAgent):
    """TODO: pull fundamentals (P/E, revenue growth, margins, cash runway).

    Planned data: SEC filings / provider API. For now emits neutral with low confidence.
    """

    agent_id = "fundamental"

    def analyze(self, ticker: str, *, benchmark: str = "SPY") -> Signal:
        snap = self.market_data.snapshot(ticker)
        return Signal(
            agent_id=self.agent_id,
            ticker=ticker,
            direction=Direction.NEUTRAL,
            confidence=10.0,
            reasoning=(
                "FundamentalAgent stub: no fundamental data wired yet. "
                "TODO: valuation multiples, growth, balance-sheet health, theme fit."
            ),
            supporting_data={
                "stub": True,
                "todos": [
                    "Wire fundamental data provider",
                    "Score valuation vs peers in theme",
                    "Flag dilution / cash burn for small-caps",
                ],
                "snapshot": snap,
                "benchmark": benchmark,
            },
        )
