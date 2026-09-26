"""Backtest research agents — live OHLCV desks + as-of/proxy replacements for stubs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from quant_system.market_data.interface import MarketData
from quant_system.models.signal import Direction, Signal
from quant_system.research.base import ResearchAgent

# Kept for unit tests / emergency fallback if as-of cache is empty.
class StubSentimentAgent(ResearchAgent):
    """Legacy neutral stub (prefer ProxySentimentAgent)."""

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
    """Legacy neutral stub (prefer FundamentalAgent + as-of fetcher)."""

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


def build_backtest_agents(
    market_data: MarketData,
    universe: list[str] | None = None,
    *,
    fundamentals_cache_dir: Path | str | None = None,
    fundamentals_bundles: dict[str, dict[str, Any]] | None = None,
) -> list[ResearchAgent]:
    """Live: momentum, statistical_ml, macro, as-of fundamental, proxy sentiment."""
    from quant_system.backtest.fundamentals_asof import make_asof_fundamentals_fetcher
    from quant_system.backtest.sentiment_proxy import ProxySentimentAgent
    from quant_system.research.fundamental import FundamentalAgent
    from quant_system.research.macro import MacroAgent
    from quant_system.research.momentum import MomentumAgent
    from quant_system.research.statistical_ml import StatisticalMLAgent

    fetcher = make_asof_fundamentals_fetcher(
        market_data,
        cache_dir=fundamentals_cache_dir,
        bundles=fundamentals_bundles,
    )
    return [
        MomentumAgent(market_data),
        FundamentalAgent(market_data, fundamentals_fetcher=fetcher),
        StatisticalMLAgent(market_data, universe=universe),
        ProxySentimentAgent(market_data),
        MacroAgent(market_data),
    ]


AGENT_STATUS: dict[str, str] = {
    "momentum": "live (OHLCV as-of)",
    "fundamental": (
        "as-of statements (FMP filingDate when available; else yfinance frames + "
        "filing lag; cache under data/backtest/fundamentals/; no live Yahoo .info)"
    ),
    "statistical_ml": "live (OHLCV as-of)",
    "sentiment": "proxy (5d return + volume shock; not historical news)",
    "macro": "live (cached proxy bars as-of; missing proxies → soft neutral)",
}
