"""MacroAgent: regime classification from injectable proxies + missing-data path."""

from __future__ import annotations

import pytest

from quant_system.market_data.yfinance_provider import MockMarketData
from quant_system.models.signal import Direction
from quant_system.research.macro import MacroAgent, classify_regime, fetch_macro_proxies


@pytest.fixture()
def md():
    return MockMarketData(
        prices={
            "IONQ": 10.0,
            "NVDA": 120.0,
            "SPY": 450.0,
            "^VIX": 14.0,
            "UUP": 28.0,
            "TLT": 95.0,
        }
    )


def test_missing_proxies_neutral_low_confidence(md):
    agent = MacroAgent(md, proxies_fetcher=lambda _b: {})
    sig = agent.analyze("IONQ")
    assert sig.agent_id == "macro"
    assert sig.ticker == "IONQ"
    assert sig.direction == Direction.NEUTRAL
    assert sig.confidence <= 25
    assert sig.supporting_data["regime"] == "neutral"
    assert sig.supporting_data["macro_risk_level"] in ("unknown", "moderate", "low")
    assert sig.supporting_data["sector_bias"] == "balanced"
    assert "key_levels" in sig.supporting_data
    assert sig.supporting_data["stub"] is False
    assert "insufficient" in sig.reasoning.lower() or "no macro" in sig.reasoning.lower()


def test_fetcher_exception_does_not_crash(md):
    def boom(_b: str):
        raise RuntimeError("macro feed down")

    agent = MacroAgent(md, proxies_fetcher=boom)
    sig = agent.analyze("NVDA")
    assert sig.direction == Direction.NEUTRAL
    assert sig.confidence <= 25
    assert sig.supporting_data["fetch_error"]
    assert "macro feed down" in sig.supporting_data["fetch_error"]
    assert sig.supporting_data["regime"] == "neutral"


def test_risk_on_proxies_long_pro_cyclical(md):
    proxies = {
        "benchmark": "SPY",
        "vix": 12.5,
        "vix_ma20": 14.0,
        "vix_change_5d": -0.10,
        "vix_ticker": "^VIX",
        "dollar_last": 27.5,
        "dollar_ma50": 28.0,
        "dollar_change_20d": -0.03,
        "dollar_ticker": "UUP",
        "rates_last": 98.0,
        "rates_change_20d": 0.04,
        "rates_ticker": "TLT",
        "rates_is_yield": False,
        "spy_last": 500.0,
        "spy_ma50": 480.0,
        "spy_ma200": 450.0,
        "spy_change_20d": 0.05,
        "realized_vol_20d_ann": 0.12,
    }
    agent = MacroAgent(md, proxies_fetcher=lambda _b: proxies)
    sig = agent.analyze("IONQ", benchmark="SPY")
    assert sig.direction == Direction.LONG
    assert sig.supporting_data["regime"] == "risk_on"
    assert sig.supporting_data["macro_risk_level"] == "low"
    assert sig.supporting_data["sector_bias"] == "pro_cyclical"
    assert sig.supporting_data["sector_tilt"] == 1.0
    assert sig.supporting_data["theme"] == "quantum"
    assert sig.confidence >= 40
    kl = sig.supporting_data["key_levels"]
    assert kl.get("vix") == pytest.approx(12.5)
    assert "spy_ma50" in kl
    assert sig.supporting_data["stub"] is False


def test_risk_off_proxies_short_defensive(md):
    proxies = {
        "benchmark": "SPY",
        "vix": 32.0,
        "vix_ma20": 22.0,
        "vix_change_5d": 0.25,
        "vix_ticker": "^VIX",
        "dollar_last": 30.0,
        "dollar_ma50": 28.5,
        "dollar_change_20d": 0.04,
        "dollar_ticker": "UUP",
        "rates_last": 88.0,
        "rates_change_20d": -0.05,
        "rates_ticker": "TLT",
        "rates_is_yield": False,
        "spy_last": 400.0,
        "spy_ma50": 420.0,
        "spy_ma200": 440.0,
        "spy_change_20d": -0.06,
        "realized_vol_20d_ann": 0.40,
    }
    agent = MacroAgent(md, proxies_fetcher=lambda _b: proxies)
    sig = agent.analyze("NVDA")
    assert sig.direction == Direction.SHORT
    assert sig.supporting_data["regime"] == "risk_off"
    assert sig.supporting_data["macro_risk_level"] == "high"
    assert sig.supporting_data["sector_bias"] == "defensive"
    assert sig.supporting_data["sector_tilt"] == -1.0
    assert sig.supporting_data["theme"] == "robotics"
    assert sig.confidence >= 40


def test_classify_regime_empty():
    out = classify_regime({})
    assert out["regime"] == "neutral"
    assert out["direction"] == Direction.NEUTRAL
    assert out["confidence"] <= 25
    assert out["macro_risk_level"] == "unknown"


def test_partial_proxies_still_emits(md):
    """Only VIX present — should not crash; may be neutral/low-ish confidence."""
    agent = MacroAgent(
        md,
        proxies_fetcher=lambda _b: {"vix": 18.0, "vix_ticker": "^VIX", "benchmark": "SPY"},
    )
    sig = agent.analyze("RKLB")
    assert sig.direction in (Direction.LONG, Direction.SHORT, Direction.NEUTRAL)
    assert 0 <= sig.confidence <= 100
    assert sig.supporting_data["regime"] in ("risk_on", "risk_off", "neutral")
    assert "sector_bias" in sig.supporting_data
    assert sig.supporting_data["theme"] == "space"
    assert sig.supporting_data["coverage"] >= 1


def test_fetch_macro_proxies_via_mock_market_data(md):
    """Default path uses MarketData histories (MockMarketData always returns bars)."""
    proxies = fetch_macro_proxies(md, benchmark="SPY")
    assert proxies.get("spy_last") is not None
    # Mock generates series for any ticker including ^VIX / UUP / TLT
    assert proxies.get("vix") is not None or "vix" in (proxies.get("errors") or {})
    agent = MacroAgent(md)  # no injector — live path through mock
    sig = agent.analyze("IONQ", benchmark="SPY")
    assert sig.agent_id == "macro"
    assert sig.supporting_data["regime"] in ("risk_on", "risk_off", "neutral")
    assert "key_levels" in sig.supporting_data
    assert "macro_risk_level" in sig.supporting_data
    assert "sector_bias" in sig.supporting_data
