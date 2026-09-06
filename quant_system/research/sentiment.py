"""SentimentAgent — news sentiment stub with credibility + manipulation_risk."""

from __future__ import annotations

from quant_system.market_data.interface import MarketData
from quant_system.models.signal import Direction, Signal
from quant_system.research.base import ResearchAgent


class SentimentAgent(ResearchAgent):
    """TODO: ingest news headlines; score polarity; flag low-credibility / pump risk.

    Always includes credibility and manipulation_risk in supporting_data.
    """

    agent_id = "sentiment"

    def analyze(self, ticker: str, *, benchmark: str = "SPY") -> Signal:
        # Stub: no live news feed yet
        credibility = 0.0  # 0-1; unknown source
        manipulation_risk = True  # conservative until feed wired
        return Signal(
            agent_id=self.agent_id,
            ticker=ticker,
            direction=Direction.NEUTRAL,
            confidence=5.0,
            reasoning=(
                "SentimentAgent stub: no news feed configured. "
                "Emitting neutral with manipulation_risk=true until credibility pipeline is live."
            ),
            supporting_data={
                "stub": True,
                "credibility": credibility,
                "manipulation_risk": manipulation_risk,
                "headlines": [],
                "todos": [
                    "Wire news API (e.g. Finnhub/NewsAPI) for theme tickers",
                    "Credibility scoring by source domain",
                    "Pump/dump heuristic on volume+headline bursts",
                    "Never trade solely on low-credibility sentiment",
                ],
                "benchmark": benchmark,
            },
        )
