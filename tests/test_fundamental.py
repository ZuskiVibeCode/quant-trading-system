"""FundamentalAgent: missing-data path + mocked rich fundamentals."""

from __future__ import annotations

import pytest

from quant_system.market_data.yfinance_provider import MockMarketData
from quant_system.models.signal import Direction
from quant_system.research.fundamental import FundamentalAgent, _score_fundamentals


@pytest.fixture()
def md():
    return MockMarketData(prices={"IBM": 180.0, "NVDA": 120.0, "IONQ": 10.0, "MISS": 5.0})


def test_missing_data_neutral_low_confidence(md):
    agent = FundamentalAgent(md, fundamentals_fetcher=lambda _t: {})
    sig = agent.analyze("MISS")
    assert sig.agent_id == "fundamental"
    assert sig.ticker == "MISS"
    assert sig.direction == Direction.NEUTRAL
    assert sig.confidence <= 25
    assert "insufficient" in sig.reasoning.lower() or "missing" in sig.reasoning.lower() or "no " in sig.reasoning.lower()
    assert sig.supporting_data["stub"] is False
    assert sig.supporting_data["quality_score"] == pytest.approx(50.0)
    assert "insufficient" in sig.supporting_data["valuation_note"].lower()
    assert sig.created_at


def test_fetcher_exception_does_not_crash(md):
    def boom(_t: str):
        raise RuntimeError("provider down")

    agent = FundamentalAgent(md, fundamentals_fetcher=boom)
    sig = agent.analyze("IBM")
    assert sig.direction == Direction.NEUTRAL
    assert sig.confidence <= 25
    assert sig.supporting_data["fetch_error"]
    assert "provider down" in sig.supporting_data["fetch_error"]


def test_rich_growth_quality_long(md):
    rich = {
        "revenueGrowth": 0.55,
        "earningsGrowth": 0.80,
        "profitMargins": 0.35,
        "operatingMargins": 0.40,
        "freeCashflow": 25_000_000_000,
        "totalRevenue": 80_000_000_000,
        "debtToEquity": 25.0,  # ~0.25x after /100
        "currentRatio": 3.5,
        "returnOnEquity": 0.45,
        "trailingPE": 22.0,
        "priceToSalesTrailing12Months": 12.0,
        "pegRatio": 0.7,
    }
    agent = FundamentalAgent(md, fundamentals_fetcher=lambda _t: rich)
    sig = agent.analyze("NVDA")
    assert sig.direction == Direction.LONG
    assert sig.confidence >= 40
    assert sig.supporting_data["quality_score"] >= 70
    assert "metrics" in sig.supporting_data
    assert sig.supporting_data["metrics"].get("revenueGrowth") == pytest.approx(0.55)
    assert sig.supporting_data["stub"] is False
    assert "valuation_note" in sig.supporting_data
    assert sig.supporting_data["valuation_note"]


def test_weak_fundamentals_short(md):
    weak = {
        "revenueGrowth": -0.25,
        "earningsGrowth": -0.40,
        "profitMargins": -0.15,
        "operatingMargins": -0.30,
        "freeCashflow": -500_000_000,
        "totalRevenue": 1_000_000_000,
        "debtToEquity": 350.0,  # ~3.5x
        "currentRatio": 0.6,
        "returnOnEquity": -0.35,
        "trailingPE": 80.0,
        "priceToSalesTrailing12Months": 45.0,
    }
    agent = FundamentalAgent(md, fundamentals_fetcher=lambda _t: weak)
    sig = agent.analyze("IONQ")
    assert sig.direction == Direction.SHORT
    assert sig.confidence >= 35
    assert sig.supporting_data["quality_score"] < 50
    assert "cash burn" in sig.reasoning.lower() or "negative" in sig.reasoning.lower()


def test_score_fundamentals_empty():
    out = _score_fundamentals({})
    assert out["direction"] == Direction.NEUTRAL
    assert out["confidence"] <= 20
    assert out["quality_score"] == 50.0
