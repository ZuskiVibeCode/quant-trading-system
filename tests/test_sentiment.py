"""SentimentAgent: no-news / normal / pump heuristics + auditor gating."""

from __future__ import annotations

import pytest

from quant_system.config.loader import load_config
from quant_system.market_data.yfinance_provider import MockMarketData
from quant_system.models.signal import Direction
from quant_system.oversight.auditor import Auditor
from quant_system.research.sentiment import SentimentAgent, score_headlines
from quant_system.tracker.api import Tracker


@pytest.fixture()
def md():
    return MockMarketData(prices={"IONQ": 10.0, "NVDA": 20.0, "PUMP": 5.0})


def test_no_news_no_manipulation_flag(md):
    agent = SentimentAgent(md, news_fetcher=lambda _t: [])
    sig = agent.analyze("IONQ")
    assert sig.direction == Direction.NEUTRAL
    assert sig.supporting_data["manipulation_risk"] is False
    assert sig.supporting_data["credibility"] == pytest.approx(0.5)
    assert sig.supporting_data["headline_count"] == 0
    assert "no" in sig.reasoning.lower() and "headline" in sig.reasoning.lower()


def test_normal_headlines_no_flag(md):
    headlines = [
        {
            "title": "IonQ announces partnership and growth outlook",
            "summary": "Company reports strong expansion plans",
            "publisher": "Reuters",
            "credibility": 0.85,
        },
        {
            "title": "Analyst upgrade cites innovation advantage",
            "summary": "Optimistic on long-term opportunity",
            "publisher": "Bloomberg",
            "credibility": 0.85,
        },
        {
            "title": "Quantum sector sees steady gains",
            "summary": "",
            "publisher": "CNBC",
            "credibility": 0.85,
        },
    ]
    agent = SentimentAgent(md, news_fetcher=lambda _t: headlines)
    sig = agent.analyze("IONQ")
    assert sig.supporting_data["manipulation_risk"] is False
    assert sig.supporting_data["credibility"] >= 0.7
    assert sig.direction in (Direction.LONG, Direction.NEUTRAL)
    assert sig.confidence >= 20


def test_pump_heuristic_flags_risk(md):
    headlines = [
        {
            "title": "PUMP to the moon — guaranteed 100x massive gains act now",
            "summary": "Secret tip: next nvidia skyrocket free money",
            "publisher": "CryptoMoonTips",
            "credibility": 0.25,
        },
        {
            "title": "Unbelievable returns — rocket parabolic moon mission",
            "summary": "Can't miss life-changing multibagger",
            "publisher": "PennyPumpDaily",
            "credibility": 0.25,
        },
        {
            "title": "Must buy now — explode overnight double",
            "summary": "Insane gains guaranteed",
            "publisher": "HotTipz",
            "credibility": 0.25,
        },
    ]
    agent = SentimentAgent(md, news_fetcher=lambda _t: headlines)
    sig = agent.analyze("PUMP")
    assert sig.supporting_data["manipulation_risk"] is True
    assert sig.supporting_data["credibility"] < 0.4
    assert sig.supporting_data["manipulation_evidence"]


def test_auditor_skips_no_news_and_flags_pump(tmp_path, md):
    cfg = load_config()
    tracker = Tracker(str(tmp_path / "aud.db"))

    no_news = SentimentAgent(md, news_fetcher=lambda _t: []).analyze("IONQ")
    pump_headlines = [
        {
            "title": "Moon rocket guaranteed 100x massive gains",
            "summary": "Secret skyrocket free money act now",
            "publisher": "PumpChannel",
            "credibility": 0.25,
        },
        {
            "title": "Parabolic moon — can't miss life-changing returns",
            "summary": "Next tesla explode overnight",
            "publisher": "HypeBlog",
            "credibility": 0.25,
        },
        {
            "title": "Insane multibagger must buy now",
            "summary": "Guaranteed double overnight",
            "publisher": "TipsterX",
            "credibility": 0.25,
        },
    ]
    pump = SentimentAgent(md, news_fetcher=lambda _t: pump_headlines).analyze("PUMP")

    normal = SentimentAgent(
        md,
        news_fetcher=lambda _t: [
            {
                "title": "Nvidia reports record profit and growth",
                "summary": "Strong beat vs estimates",
                "publisher": "Reuters",
                "credibility": 0.85,
            }
        ],
    ).analyze("NVDA")

    auditor = Auditor(tracker, cfg)
    stored = [
        (tracker.insert_signal(no_news), no_news),
        (tracker.insert_signal(normal), normal),
        (tracker.insert_signal(pump), pump),
    ]
    flag_ids = auditor.review_signals(stored)
    flags = tracker.list_audit_flags(limit=50)
    sentiment_flags = [f for f in flags if f["flag_type"] == "sentiment_risk"]
    assert len(sentiment_flags) == 1
    assert "PUMP" in sentiment_flags[0]["message"]
    assert flag_ids
    tracker.close()


def test_score_headlines_empty_defaults():
    out = score_headlines([])
    assert out["manipulation_risk"] is False
    assert out["direction"] == Direction.NEUTRAL
    assert out["credibility"] == 0.5
