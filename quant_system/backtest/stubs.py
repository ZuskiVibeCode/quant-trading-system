"""Neutral stub research agents for signals that cannot replay cleanly on OHLCV alone."""

from __future__ import annotations

from quant_system.market_data.interface import MarketData
from quant_system.models.signal import Direction, Signal
from quant_system.research.base import ResearchAgent


class StubSentimentAgent(ResearchAgent):
    """Replaces live news-headline SentimentAgent in backtests."""

    agent_id = "sentiment"

    def analyze(self, ticker: str, *, benchmark: str = "SPY") -> Signal:
        return Signal(
            agent_id=self.agent_id,
            ticker=ticker.upper(),
            direction=Direction.NEUTRAL,
            confidence=10.0,
            reasoning="Backtest stub: no historical news replay (SentimentAgent neutralized)",
            supporting_data={
                "stubbed": True,
                "raw_score": 0.0,
                "benchmark": benchmark,
            },
        )


class StubFundamentalAgent(ResearchAgent):
    """Replaces live fundamentals FundamentalAgent in backtests."""

    agent_id = "fundamental"

    def analyze(self, ticker: str, *, benchmark: str = "SPY") -> Signal:
        return Signal(
            agent_id=self.agent_id,
            ticker=ticker.upper(),
            direction=Direction.NEUTRAL,
            confidence=10.0,
            reasoning="Backtest stub: no point-in-time fundamentals replay (FundamentalAgent neutralized)",
            supporting_data={
                "stubbed": True,
                "raw_score": 0.0,
                "benchmark": benchmark,
            },
        )


def build_backtest_agents(market_data: MarketData, universe: list[str] | None = None) -> list[ResearchAgent]:
    """Live: momentum, statistical_ml, macro. Stubbed: sentiment, fundamental."""
    from quant_system.research.macro import MacroAgent
    from quant_system.research.momentum import MomentumAgent
    from quant_system.research.statistical_ml import StatisticalMLAgent

    return [
        MomentumAgent(market_data),
        StubFundamentalAgent(market_data),
        StatisticalMLAgent(market_data, universe=universe),
        StubSentimentAgent(market_data),
        MacroAgent(market_data),
    ]


AGENT_STATUS: dict[str, str] = {
    "momentum": "live (OHLCV as-of)",
    "fundamental": "stubbed (neutral; no PIT fundamentals)",
    "statistical_ml": "live (OHLCV as-of)",
    "sentiment": "stubbed (neutral; no historical news)",
    "macro": "live (cached proxy bars as-of; missing proxies → soft neutral)",
}
