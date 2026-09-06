"""StatisticalMLAgent: mean-revert short, trending long, empty/missing paths."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_system.market_data.yfinance_provider import MockMarketData
from quant_system.models.signal import Direction
from quant_system.research.statistical_ml import (
    StatisticalMLAgent,
    _ohlcv_from_close,
    compute_stat_features,
    score_stat_features,
)


def _idx(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-02", periods=n, freq="B")


def mean_revert_overbought_close(n: int = 120) -> pd.Series:
    """Flat series then sharp spike → overbought vs MA20 (mean-revert short)."""
    arr = np.full(n, 100.0)
    arr[-5:] = [108.0, 115.0, 122.0, 128.0, 135.0]
    return pd.Series(arr, index=_idx(n))


def trending_up_close(n: int = 120) -> pd.Series:
    """Smooth linear uptrend → momentum-stat long."""
    t = np.arange(n, dtype=float)
    return pd.Series(50.0 + t * 0.4, index=_idx(n))


@pytest.fixture()
def series_map() -> dict[str, pd.DataFrame]:
    return {
        "MRST": _ohlcv_from_close(mean_revert_overbought_close()),
        "TREND": _ohlcv_from_close(trending_up_close()),
        "EMPTY": pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"]),
    }


@pytest.fixture()
def agent(series_map):
    def history_fetcher(ticker: str) -> pd.DataFrame:
        key = ticker.upper()
        if key not in series_map:
            raise KeyError(f"no series for {key}")
        return series_map[key]

    peer_moms = {"MRST": 0.35, "TREND": 0.12, "PEER": -0.05}
    return StatisticalMLAgent(
        MockMarketData(prices={"MRST": 135.0, "TREND": 98.0}),
        history_fetcher=history_fetcher,
        peer_mom_fetcher=lambda t: peer_moms.get(t.upper()),
        universe=["MRST", "TREND", "PEER"],
    )


def test_mean_revert_overbought_short(agent):
    sig = agent.analyze("MRST")
    assert sig.agent_id == "statistical_ml"
    assert sig.ticker == "MRST"
    assert sig.direction == Direction.SHORT
    assert sig.confidence >= 35
    sd = sig.supporting_data
    assert sd["stub"] is False
    assert sd["predicted_direction"] == "short"
    assert sd["statistical_edge_score"] < -12
    assert sd["z_to_ma20"] is not None and sd["z_to_ma20"] >= 1.25
    assert "confidence_interval" in sd
    assert sd["confidence_interval"]["low"] <= sig.confidence <= sd["confidence_interval"]["high"]
    assert "uncertainty_note" in sd and sd["uncertainty_note"]
    assert "overbought" in sig.reasoning.lower() or "ma20" in sig.reasoning.lower()
    assert sig.created_at


def test_trending_long(agent):
    sig = agent.analyze("TREND")
    assert sig.agent_id == "statistical_ml"
    assert sig.direction == Direction.LONG
    assert sig.confidence >= 40
    sd = sig.supporting_data
    assert sd["predicted_direction"] == "long"
    assert sd["statistical_edge_score"] >= 12
    assert sd["mom20"] is not None and sd["mom20"] > 0.03
    assert sd["mom60"] is not None and sd["mom60"] > 0.10
    assert "momentum" in sig.reasoning.lower() or "trend" in sig.reasoning.lower()
    assert "vol_regime" in sd


def test_empty_series_neutral_low_confidence(agent):
    sig = agent.analyze("EMPTY")
    assert sig.direction == Direction.NEUTRAL
    assert sig.confidence <= 15
    assert sig.supporting_data["statistical_edge_score"] == pytest.approx(0.0)
    assert sig.supporting_data["predicted_direction"] == "neutral"
    assert "insufficient" in sig.reasoning.lower() or "missing" in sig.supporting_data["uncertainty_note"].lower()


def test_history_exception_neutral(series_map):
    def boom(_t: str):
        raise RuntimeError("feed down")

    agent = StatisticalMLAgent(MockMarketData(), history_fetcher=boom)
    sig = agent.analyze("IONQ")
    assert sig.direction == Direction.NEUTRAL
    assert sig.confidence <= 15
    assert sig.supporting_data["predicted_direction"] == "neutral"


def test_compute_stat_features_empty():
    assert compute_stat_features(pd.Series(dtype=float)) == {}
    out = score_stat_features({})
    assert out["direction"] == Direction.NEUTRAL
    assert out["confidence"] <= 15
    assert out["statistical_edge_score"] == 0.0


def test_score_features_insufficient_bars():
    close = pd.Series(np.linspace(10, 11, 20))
    feat = compute_stat_features(close)
    scored = score_stat_features(feat)
    assert scored["direction"] == Direction.NEUTRAL
    assert scored["confidence"] <= 15


def test_agent_via_mock_market_data_no_crash():
    """Default path through MockMarketData (random walk) still emits a valid Signal."""
    md = MockMarketData(prices={"IONQ": 10.0, "NVDA": 120.0, "SPY": 450.0}, bars=250)
    agent = StatisticalMLAgent(md, enable_cross_sectional=False)
    sig = agent.analyze("IONQ", benchmark="SPY")
    assert sig.agent_id == "statistical_ml"
    assert sig.direction in (Direction.LONG, Direction.SHORT, Direction.NEUTRAL)
    assert 0 <= sig.confidence <= 100
    assert "statistical_edge_score" in sig.supporting_data
    assert "predicted_direction" in sig.supporting_data
    assert "uncertainty_note" in sig.supporting_data
    assert "confidence_interval" in sig.supporting_data
