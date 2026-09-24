"""FundamentalAgent v2: EV math, multiples, dilution soft flags, missing-data paths."""

from __future__ import annotations

import pytest

from quant_system.market_data.yfinance_provider import MockMarketData
from quant_system.models.signal import Direction
from quant_system.research.fundamental import (
    FundamentalAgent,
    _score_fundamentals,
    dilution_soft_flags,
    fetch_fmp_fundamentals,
    fetch_fundamentals_with_fallback,
    rebuild_capital_structure,
    sales_multiples,
)


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
    assert "insufficient" in sig.reasoning.lower()
    assert "Quality:" in sig.reasoning or "quality" in sig.reasoning.lower()
    assert "Valuation/EV:" in sig.reasoning or "valuation" in sig.reasoning.lower()
    assert sig.supporting_data["stub"] is False
    assert sig.supporting_data["quality_score"] == pytest.approx(50.0)
    assert "insufficient" in sig.supporting_data["valuation_note"].lower()
    assert sig.supporting_data.get("market_cap") is None
    assert sig.supporting_data.get("ev") is None
    assert sig.supporting_data.get("dilution_flag") is False
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
        "marketCap": 2_000_000_000_000,
        "totalCash": 50_000_000_000,
        "totalDebt": 10_000_000_000,
        "sharesOutstanding": 25_000_000_000,
    }
    agent = FundamentalAgent(md, fundamentals_fetcher=lambda _t: rich)
    sig = agent.analyze("NVDA")
    assert sig.direction == Direction.LONG
    assert sig.confidence >= 40
    assert sig.supporting_data["quality_score"] >= 70
    assert "metrics" in sig.supporting_data
    assert sig.supporting_data["metrics"].get("revenueGrowth") == pytest.approx(0.55)
    assert sig.supporting_data["stub"] is False
    assert sig.supporting_data["valuation_note"]
    assert sig.supporting_data["market_cap"] == pytest.approx(2_000_000_000_000)
    assert sig.supporting_data["cash"] == pytest.approx(50_000_000_000)
    assert sig.supporting_data["debt"] == pytest.approx(10_000_000_000)
    assert sig.supporting_data["ev"] is not None
    assert sig.supporting_data["shares_out"] == pytest.approx(25_000_000_000)
    assert "Quality:" in sig.reasoning
    assert "Valuation/EV:" in sig.reasoning
    assert "Bear/risk:" in sig.reasoning


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
        "marketCap": 5_000_000_000,
        "totalCash": 200_000_000,
        "totalDebt": 1_500_000_000,
        "sharesOutstanding": 500_000_000,
    }
    agent = FundamentalAgent(md, fundamentals_fetcher=lambda _t: weak)
    sig = agent.analyze("IONQ")
    assert sig.direction == Direction.SHORT
    assert sig.confidence >= 35
    assert sig.supporting_data["quality_score"] < 50
    assert "cash burn" in sig.reasoning.lower() or "negative" in sig.reasoning.lower() or "bear" in sig.reasoning.lower()
    assert sig.supporting_data["ev"] is not None
    assert sig.supporting_data.get("multiple_extreme") is True or (
        sig.supporting_data.get("price_to_sales") or 0
    ) >= 20


def test_score_fundamentals_empty():
    out = _score_fundamentals({})
    assert out["direction"] == Direction.NEUTRAL
    assert out["confidence"] <= 20
    assert out["quality_score"] == 50.0
    assert "insufficient" in out["desk_reasoning"].lower()


def test_rebuild_ev_estimate():
    cap = rebuild_capital_structure(
        {
            "marketCap": 1_000_000_000,
            "totalCash": 200_000_000,
            "totalDebt": 100_000_000,
            "sharesOutstanding": 50_000_000,
        }
    )
    assert cap["market_cap"] == pytest.approx(1_000_000_000)
    assert cap["cash"] == pytest.approx(200_000_000)
    assert cap["debt"] == pytest.approx(100_000_000)
    assert cap["shares_out"] == pytest.approx(50_000_000)
    assert cap["ev"] == pytest.approx(900_000_000)  # 1B + 100M - 200M
    assert cap["ev_source"] == "estimate"
    assert cap["net_cash"] == pytest.approx(100_000_000)


def test_rebuild_ev_prefers_vendor():
    cap = rebuild_capital_structure(
        {
            "marketCap": 1_000_000_000,
            "totalCash": 200_000_000,
            "totalDebt": 100_000_000,
            "enterpriseValue": 950_000_000,
        }
    )
    assert cap["ev"] == pytest.approx(950_000_000)
    assert cap["ev_source"] == "yfinance"
    assert cap["ev_estimate"] == pytest.approx(900_000_000)


def test_rebuild_ev_missing_cash_debt_proxy():
    cap = rebuild_capital_structure({"marketCap": 500_000_000})
    assert cap["ev"] == pytest.approx(500_000_000)
    assert cap["ev_source"] == "market_cap_proxy"
    assert cap["cash"] is None
    assert cap["debt"] is None


def test_rebuild_missing_all():
    cap = rebuild_capital_structure({})
    assert cap["market_cap"] is None
    assert cap["ev"] is None
    assert cap["ev_source"] is None


def test_sales_multiples_from_ev_and_revenue():
    capital = rebuild_capital_structure(
        {
            "marketCap": 2_000_000_000,
            "totalCash": 100_000_000,
            "totalDebt": 50_000_000,
        }
    )
    mult = sales_multiples(
        {"totalRevenue": 100_000_000, "revenueGrowth": 0.40},
        capital,
    )
    assert mult["revenue"] == pytest.approx(100_000_000)
    assert mult["revenue_growth"] == pytest.approx(0.40)
    # EV = 2B + 50M - 100M = 1.95B → EV/sales = 19.5
    assert mult["ev_to_sales"] == pytest.approx(19.5)
    assert mult["price_to_sales"] == pytest.approx(20.0)
    assert mult["multiple_extreme"] is True  # P/S 20 extreme; EV/sales elevated
    assert mult["multiple_note"]


def test_sales_multiples_missing_revenue():
    capital = rebuild_capital_structure({"marketCap": 1_000_000_000, "totalDebt": 0, "totalCash": 0})
    mult = sales_multiples({}, capital)
    assert mult["ev_to_sales"] is None
    assert mult["price_to_sales"] is None
    assert mult["multiple_extreme"] is False


def test_dilution_share_count_gap_soft():
    flags = dilution_soft_flags(
        {
            "sharesOutstanding": 100_000_000,
            "marketCap": 1_200_000_000,
            "currentPrice": 10.0,  # implied 120M = +20% gap
        }
    )
    assert flags["dilution_flag"] is True
    assert flags["implied_shares"] == pytest.approx(120_000_000)
    assert flags["share_count_gap_pct"] == pytest.approx(0.20)
    assert any("share-count" in r.lower() or "implied" in r.lower() for r in flags["dilution_reasons"])


def test_dilution_tight_float_soft():
    flags = dilution_soft_flags(
        {
            "sharesOutstanding": 100_000_000,
            "floatShares": 40_000_000,
        }
    )
    assert flags["dilution_flag"] is True
    assert flags["float_to_shares"] == pytest.approx(0.40)
    assert any("float" in r.lower() for r in flags["dilution_reasons"])


def test_dilution_text_warrant_soft_no_invention():
    flags = dilution_soft_flags(
        {
            "sharesOutstanding": 50_000_000,
            "_info_text": "The company may issue warrants and convertible notes.",
        }
    )
    assert flags["dilution_flag"] is True
    assert flags["text_dilution_hit"] is True
    # No shares gap without price/mcap
    assert flags["share_count_gap_pct"] is None


def test_dilution_no_flag_when_aligned():
    flags = dilution_soft_flags(
        {
            "sharesOutstanding": 100_000_000,
            "floatShares": 80_000_000,
            "marketCap": 1_000_000_000,
            "currentPrice": 10.0,  # implied == shares
            "longBusinessSummary": "Software company selling cloud services.",
        }
    )
    assert flags["dilution_flag"] is False
    assert flags["dilution_reasons"] == []


def test_dilution_soft_effect_on_score(md):
    """Dilution flag should soften score vs identical name without the gap (soft only)."""
    base = {
        "revenueGrowth": 0.10,
        "profitMargins": 0.05,
        "freeCashflow": 10_000_000,
        "totalRevenue": 500_000_000,
        "debtToEquity": 40.0,
        "currentRatio": 1.8,
        "trailingPE": 20.0,
        "priceToSalesTrailing12Months": 4.0,
        "marketCap": 2_000_000_000,
        "totalCash": 100_000_000,
        "totalDebt": 50_000_000,
        "sharesOutstanding": 100_000_000,
        "currentPrice": 20.0,  # implied == shares
    }
    diluted = dict(base)
    diluted["currentPrice"] = 15.0  # implied 133.3M → ~33% gap

    clean = _score_fundamentals(base)
    dirty = _score_fundamentals(diluted)
    assert dirty["dilution"]["dilution_flag"] is True
    assert clean["dilution"]["dilution_flag"] is False
    assert dirty["raw_score"] < clean["raw_score"]
    # Soft magnitude: not a hard block — score delta typically a handful of points
    assert clean["raw_score"] - dirty["raw_score"] <= 12
    assert clean["raw_score"] - dirty["raw_score"] >= 3


def test_extreme_multiple_soft_bear_in_reasoning(md):
    theme_rich = {
        "revenueGrowth": 0.30,
        "profitMargins": -0.40,
        "operatingMargins": -0.50,
        "freeCashflow": -80_000_000,
        "totalRevenue": 50_000_000,
        "debtToEquity": 5.0,
        "currentRatio": 2.0,
        "marketCap": 3_000_000_000,
        "totalCash": 400_000_000,
        "totalDebt": 50_000_000,
        "sharesOutstanding": 200_000_000,
        "priceToSalesTrailing12Months": 60.0,
    }
    agent = FundamentalAgent(md, fundamentals_fetcher=lambda _t: theme_rich)
    sig = agent.analyze("IONQ")
    assert sig.supporting_data["ev"] is not None
    assert sig.supporting_data["ev_to_sales"] is not None or sig.supporting_data["price_to_sales"] is not None
    assert "Bear/risk:" in sig.reasoning
    # Soft only — still a normal Signal, no hard block field
    assert "hard_block" not in sig.supporting_data


def test_metrics_from_statements_maps_annual_frames():
    import pandas as pd

    from quant_system.research.fundamental import metrics_from_statements

    cols = pd.to_datetime(["2025-12-31", "2024-12-31"])
    income = pd.DataFrame(
        {
            cols[0]: {
                "Total Revenue": 200.0,
                "Net Income": 40.0,
                "Operating Income": 50.0,
                "Gross Profit": 120.0,
            },
            cols[1]: {
                "Total Revenue": 100.0,
                "Net Income": 20.0,
                "Operating Income": 25.0,
                "Gross Profit": 60.0,
            },
        }
    )
    balance = pd.DataFrame(
        {
            cols[0]: {
                "Total Debt": 30.0,
                "Cash And Cash Equivalents": 10.0,
                "Ordinary Shares Number": 5.0,
                "Stockholders Equity": 80.0,
                "Total Assets": 150.0,
            },
            cols[1]: {
                "Total Debt": 25.0,
                "Cash And Cash Equivalents": 8.0,
                "Ordinary Shares Number": 5.0,
                "Stockholders Equity": 70.0,
                "Total Assets": 140.0,
            },
        }
    )
    cashflow = pd.DataFrame(
        {
            cols[0]: {"Free Cash Flow": 35.0, "Operating Cash Flow": 45.0},
            cols[1]: {"Free Cash Flow": 18.0, "Operating Cash Flow": 22.0},
        }
    )
    fast = {"market_cap": 1000.0, "last_price": 200.0, "previous_close": 198.0}

    m = metrics_from_statements(income, balance, cashflow, fast_info=fast)
    assert m["totalRevenue"] == pytest.approx(200.0)
    assert m["freeCashflow"] == pytest.approx(35.0)
    assert m["operatingCashflow"] == pytest.approx(45.0)
    assert m["totalDebt"] == pytest.approx(30.0)
    assert m["totalCash"] == pytest.approx(10.0)
    assert m["sharesOutstanding"] == pytest.approx(5.0)
    assert m["profitMargins"] == pytest.approx(0.20)
    assert m["operatingMargins"] == pytest.approx(0.25)
    assert m["grossMargins"] == pytest.approx(0.60)
    assert m["revenueGrowth"] == pytest.approx(1.0)  # 200/100 - 1
    assert m["earningsGrowth"] == pytest.approx(1.0)
    assert m["returnOnEquity"] == pytest.approx(40.0 / 80.0)
    assert m["returnOnAssets"] == pytest.approx(40.0 / 150.0)
    assert m["debtToEquity"] == pytest.approx((30.0 / 80.0) * 100.0)
    assert m["marketCap"] == pytest.approx(1000.0)
    assert m["enterpriseValue"] == pytest.approx(1000.0 + 30.0 - 10.0)
    assert m["priceToSalesTrailing12Months"] == pytest.approx(1000.0 / 200.0)
    assert m["enterpriseToRevenue"] == pytest.approx(1020.0 / 200.0)
    assert m["currentPrice"] == pytest.approx(200.0)


def test_metrics_from_statements_net_debt_plus_cash():
    import pandas as pd

    from quant_system.research.fundamental import metrics_from_statements

    cols = pd.to_datetime(["2025-12-31"])
    balance = pd.DataFrame(
        {
            cols[0]: {
                "Net Debt": 40.0,
                "Cash And Cash Equivalents": 15.0,
                "Stockholders Equity": 100.0,
            }
        }
    )
    income = pd.DataFrame({cols[0]: {"Total Revenue": 50.0, "Net Income": 5.0}})
    m = metrics_from_statements(income, balance, None, fast_info={"marketCap": 200.0})
    assert m["totalDebt"] == pytest.approx(55.0)  # net debt + cash
    assert m["totalCash"] == pytest.approx(15.0)


def test_metrics_from_statements_empty_returns_empty():
    from quant_system.research.fundamental import metrics_from_statements

    assert metrics_from_statements(None, None, None) == {}
    assert metrics_from_statements(None, None, None, fast_info={"last_price": 10.0}) == {}


def test_agent_surfaces_fundamentals_source(md):
    payload = {
        "revenueGrowth": 0.12,
        "profitMargins": 0.10,
        "freeCashflow": 1_000_000,
        "totalRevenue": 10_000_000,
        "marketCap": 50_000_000,
        "totalCash": 2_000_000,
        "totalDebt": 1_000_000,
        "fundamentals_source": "yfinance_statements",
    }
    agent = FundamentalAgent(md, fundamentals_fetcher=lambda _t: dict(payload))
    sig = agent.analyze("IBM")
    assert sig.supporting_data["fundamentals_source"] == "yfinance_statements"
    assert "fundamentals_source" not in (sig.supporting_data.get("metrics") or {})


# ---------------------------------------------------------------------------
# FMP fallback (Yahoo primary; mock HTTP — no live network)
# ---------------------------------------------------------------------------


def _rich_yahoo_payload() -> dict:
    return {
        "revenueGrowth": 0.25,
        "earningsGrowth": 0.30,
        "profitMargins": 0.20,
        "operatingMargins": 0.22,
        "freeCashflow": 5_000_000_000,
        "totalRevenue": 40_000_000_000,
        "debtToEquity": 40.0,
        "currentRatio": 2.0,
        "returnOnEquity": 0.25,
        "trailingPE": 18.0,
        "priceToSalesTrailing12Months": 5.0,
        "marketCap": 200_000_000_000,
        "totalCash": 20_000_000_000,
        "totalDebt": 8_000_000_000,
        "sharesOutstanding": 5_000_000_000,
        "fundamentals_source": "yfinance_info",
    }


def _thin_yahoo_payload() -> dict:
    """Meaningful but below rich bar (core coverage < 4)."""
    return {
        "profitMargins": 0.12,
        "trailingPE": 22.0,
        "marketCap": 50_000_000_000,
        "fundamentals_source": "yfinance_info",
    }


def _rich_fmp_mapped() -> dict:
    return {
        "marketCap": 180_000_000_000,
        "enterpriseValue": 190_000_000_000,
        "totalRevenue": 35_000_000_000,
        "freeCashflow": 4_000_000_000,
        "totalCash": 15_000_000_000,
        "totalDebt": 10_000_000_000,
        "profitMargins": 0.18,
        "operatingMargins": 0.20,
        "returnOnEquity": 0.22,
        "debtToEquity": 50.0,
        "trailingPE": 20.0,
        "priceToSalesTrailing12Months": 5.1,
        "currentRatio": 1.8,
        "sharesOutstanding": 4_500_000_000,
        "fundamentals_source": "fmp",
    }


def test_fmp_no_api_key_returns_empty(monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    assert fetch_fmp_fundamentals("AAPL") == {}
    assert fetch_fmp_fundamentals("AAPL", api_key="") == {}
    assert fetch_fmp_fundamentals("AAPL", api_key="   ") == {}


def test_fmp_http_failure_returns_empty(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "test-key-not-real")
    monkeypatch.setattr(
        "quant_system.research.fundamental._fmp_http_get_json",
        lambda *_a, **_k: None,
    )
    assert fetch_fmp_fundamentals("AAPL") == {}


def test_fmp_maps_profile_metrics_ratios(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "test-key-not-real")

    def fake_get(url: str, *, timeout: float = 12.0):
        if "/profile/" in url:
            return [{"mktCap": 1_000_000_000, "price": 50.0, "description": "Test Co"}]
        if "/key-metrics-ttm/" in url:
            return [
                {
                    "peRatioTTM": 15.0,
                    "priceToSalesRatioTTM": 3.0,
                    "enterpriseValueTTM": 1_100_000_000,
                    "debtToEquityTTM": 0.5,
                    "roeTTM": 0.18,
                    "currentRatioTTM": 2.1,
                }
            ]
        if "/ratios-ttm/" in url:
            return [
                {
                    "netProfitMarginTTM": 0.14,
                    "operatingProfitMarginTTM": 0.16,
                    "grossProfitMarginTTM": 0.40,
                    "pegRatioTTM": 1.2,
                }
            ]
        if "/income-statement/" in url:
            return [{"revenue": 400_000_000, "netIncome": 56_000_000, "weightedAverageShsOut": 20_000_000}]
        if "/balance-sheet-statement/" in url:
            return [
                {
                    "cashAndCashEquivalents": 80_000_000,
                    "totalDebt": 40_000_000,
                    "totalStockholdersEquity": 200_000_000,
                    "totalAssets": 500_000_000,
                }
            ]
        if "/cash-flow-statement/" in url:
            return [{"freeCashFlow": 70_000_000, "operatingCashFlow": 90_000_000}]
        return None

    monkeypatch.setattr("quant_system.research.fundamental._fmp_http_get_json", fake_get)
    out = fetch_fmp_fundamentals("IBM")
    assert out["fundamentals_source"] == "fmp"
    assert out["marketCap"] == pytest.approx(1_000_000_000)
    assert out["trailingPE"] == pytest.approx(15.0)
    assert out["totalRevenue"] == pytest.approx(400_000_000)
    assert out["freeCashflow"] == pytest.approx(70_000_000)
    assert out["totalCash"] == pytest.approx(80_000_000)
    assert out["totalDebt"] == pytest.approx(40_000_000)
    assert out["profitMargins"] == pytest.approx(0.14)
    assert out["debtToEquity"] == pytest.approx(50.0)  # 0.5 ratio → yfinance-style *100
    assert out["returnOnEquity"] == pytest.approx(0.18)


def test_fallback_yahoo_rich_never_calls_fmp(monkeypatch):
    calls = {"fmp": 0}

    monkeypatch.setattr(
        "quant_system.research.fundamental.fetch_yfinance_fundamentals",
        lambda _t: _rich_yahoo_payload(),
    )

    def boom_fmp(_t, api_key=None):
        calls["fmp"] += 1
        raise AssertionError("FMP should not be called when Yahoo is rich")

    monkeypatch.setattr("quant_system.research.fundamental.fetch_fmp_fundamentals", boom_fmp)
    out = fetch_fundamentals_with_fallback("NVDA")
    assert calls["fmp"] == 0
    assert out["fundamentals_source"] == "yfinance_info"
    assert out["revenueGrowth"] == pytest.approx(0.25)


def test_fallback_yahoo_empty_fmp_rich(monkeypatch, md):
    monkeypatch.setattr(
        "quant_system.research.fundamental.fetch_yfinance_fundamentals",
        lambda _t: {},
    )
    monkeypatch.setattr(
        "quant_system.research.fundamental.fetch_fmp_fundamentals",
        lambda _t, api_key=None: _rich_fmp_mapped(),
    )
    out = fetch_fundamentals_with_fallback("IBM")
    assert out["fundamentals_source"] == "fmp"
    assert out["totalRevenue"] == pytest.approx(35_000_000_000)

    agent = FundamentalAgent(md, fundamentals_fetcher=fetch_fundamentals_with_fallback)
    sig = agent.analyze("IBM")
    assert sig.supporting_data["fundamentals_source"] == "fmp"
    # Must not stay stuck at insufficient-data neutral-15
    assert sig.confidence > 20
    assert "insufficient" not in sig.reasoning.lower() or sig.supporting_data["coverage"] > 0
    assert sig.supporting_data["quality_score"] != 50 or sig.direction != Direction.NEUTRAL or sig.confidence > 15


def test_fallback_yahoo_thin_fmp_fills_yahoo_fmp(monkeypatch):
    thin = _thin_yahoo_payload()
    fmp = _rich_fmp_mapped()
    monkeypatch.setattr(
        "quant_system.research.fundamental.fetch_yfinance_fundamentals",
        lambda _t: dict(thin),
    )
    monkeypatch.setattr(
        "quant_system.research.fundamental.fetch_fmp_fundamentals",
        lambda _t, api_key=None: dict(fmp),
    )
    out = fetch_fundamentals_with_fallback("IBM")
    assert out["fundamentals_source"] == "yahoo_fmp"
    # Yahoo wins on overlap
    assert out["profitMargins"] == pytest.approx(0.12)
    assert out["trailingPE"] == pytest.approx(22.0)
    # FMP fills gaps
    assert out["totalRevenue"] == pytest.approx(35_000_000_000)
    assert out["freeCashflow"] == pytest.approx(4_000_000_000)
    assert out["totalDebt"] == pytest.approx(10_000_000_000)


def test_fallback_both_empty(monkeypatch):
    monkeypatch.setattr(
        "quant_system.research.fundamental.fetch_yfinance_fundamentals",
        lambda _t: {},
    )
    monkeypatch.setattr(
        "quant_system.research.fundamental.fetch_fmp_fundamentals",
        lambda _t, api_key=None: {},
    )
    assert fetch_fundamentals_with_fallback("ZZZZ") == {}


def test_agent_default_uses_fallback(monkeypatch, md):
    """Default FundamentalAgent should route through Yahoo→FMP fallback."""
    seen = {"fallback": 0}

    def fake_fallback(ticker: str):
        seen["fallback"] += 1
        return {
            "profitMargins": 0.10,
            "totalRevenue": 1_000_000,
            "freeCashflow": 100_000,
            "totalCash": 50_000,
            "totalDebt": 20_000,
            "marketCap": 5_000_000,
            "fundamentals_source": "fmp",
        }

    monkeypatch.setattr(
        "quant_system.research.fundamental.fetch_fundamentals_with_fallback",
        fake_fallback,
    )
    agent = FundamentalAgent(md)  # default fetcher
    sig = agent.analyze("IBM")
    assert seen["fallback"] == 1
    assert sig.supporting_data["fundamentals_source"] == "fmp"
