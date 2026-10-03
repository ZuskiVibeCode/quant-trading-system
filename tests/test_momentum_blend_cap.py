"""Momentum absolute-share cap on the PM blended score."""

from __future__ import annotations

import pytest

from quant_system.config.loader import load_config
from quant_system.execution.blend_score import (
    DEFAULT_MOMENTUM_MAX_ABS_SHARE,
    apply_momentum_abs_share_cap,
)
from quant_system.execution.portfolio_manager import PortfolioManager
from quant_system.market_data.yfinance_provider import MockMarketData
from quant_system.models.signal import Direction, Signal
from quant_system.research.trade_review import recompute_entry_score
from quant_system.tracker.api import Tracker


OTHER_DESKS = ("fundamental", "statistical_ml", "sentiment", "macro")


@pytest.fixture()
def env(tmp_path):
    db = tmp_path / "blend_cap.db"
    tracker = Tracker(str(db))
    md = MockMarketData(prices={"AAA": 10.0, "SPY": 100.0})
    cfg = load_config()
    cfg.risk.min_confidence_to_trade = 1.0
    cfg.risk.min_confidence_to_sell = 1.0
    cfg.risk.min_abs_score = 0.01
    cfg.risk.min_order_notional = 1.0
    return tracker, md, cfg


def _sig(agent_id: str, direction: Direction, confidence: float, ticker: str = "AAA") -> Signal:
    return Signal(
        agent_id=agent_id,
        ticker=ticker,
        direction=direction,
        confidence=confidence,
        reasoning=f"test {agent_id}",
        supporting_data={"raw_score": 0.0},
    )


def test_config_exposes_momentum_max_abs_share():
    cfg = load_config()
    assert cfg.risk.momentum_max_abs_share == DEFAULT_MOMENTUM_MAX_ABS_SHARE
    assert cfg.risk.min_confidence_to_trade == 55.0
    assert cfg.risk.min_confidence_to_sell == 40.0
    assert cfg.risk.min_abs_score == 0.05


def test_cap_helper_binds_when_momentum_extreme_others_mild():
    detail = [
        {"agent_id": "momentum", "contrib": 0.19},
        {"agent_id": "fundamental", "contrib": 0.05},
        {"agent_id": "statistical_ml", "contrib": 0.05},
        {"agent_id": "sentiment", "contrib": 0.05},
        {"agent_id": "macro", "contrib": 0.05},
    ]
    score, out, meta = apply_momentum_abs_share_cap(detail, max_share=0.40)
    assert meta["momentum_capped"] is True
    assert meta["momentum_abs_share_raw"] > 0.40
    assert abs(meta["momentum_abs_share"] - 0.40) < 1e-9
    mom = next(r for r in out if r["agent_id"] == "momentum")
    assert mom["capped"] is True
    assert abs(mom["contrib"]) < abs(mom["contrib_raw"])
    assert abs(score - (mom["contrib"] + 0.20)) < 1e-12


def test_cap_skipped_when_other_desks_zero():
    detail = [
        {"agent_id": "momentum", "contrib": 0.19},
        {"agent_id": "fundamental", "contrib": 0.0},
        {"agent_id": "macro", "contrib": 0.0},
    ]
    score, out, meta = apply_momentum_abs_share_cap(detail, max_share=0.40)
    assert meta["momentum_capped"] is False
    assert meta.get("cap_skipped_reason") == "no_other_desk_abs_contrib"
    assert score == pytest.approx(0.19)
    assert out[0]["contrib"] == 0.19


def test_pm_cap_binds_extreme_momentum_mild_others(env):
    tracker, md, cfg = env
    for aid in ("momentum", *OTHER_DESKS):
        tracker.set_weight(aid, 0.2, notes="test equal")

    items = []
    for sig in [
        _sig("momentum", Direction.LONG, 95.0),
        _sig("fundamental", Direction.LONG, 25.0),
        _sig("statistical_ml", Direction.LONG, 25.0),
        _sig("sentiment", Direction.LONG, 25.0),
        _sig("macro", Direction.LONG, 25.0),
    ]:
        sid = tracker.insert_signal(sig)
        items.append((sid, sig))

    pm = PortfolioManager(tracker, md, cfg)
    weights = tracker.get_weights()
    plan = pm._score_ticker("AAA", items, weights, equity=100.0, cash=100.0, positions={})
    cap = plan.snapshot.get("momentum_cap") or {}
    assert cap.get("momentum_capped") is True
    assert abs(float(cap["momentum_abs_share"]) - 0.40) < 1e-6
    expected_other = 4 * (25.0 / 100.0) * 0.2
    expected_mom = (0.40 / 0.60) * expected_other
    assert plan.score == pytest.approx(expected_mom + expected_other, rel=1e-6)
    # Live floors on disk unchanged
    live = load_config()
    assert live.risk.min_confidence_to_trade == 55.0
    assert live.risk.min_confidence_to_sell == 40.0
    assert live.risk.min_abs_score == 0.05


def test_pm_no_cap_when_others_neutral(env):
    tracker, md, cfg = env
    for aid in ("momentum", *OTHER_DESKS):
        tracker.set_weight(aid, 0.2, notes="test equal")
    items = []
    for sig in [
        _sig("momentum", Direction.LONG, 90.0),
        _sig("fundamental", Direction.NEUTRAL, 40.0),
        _sig("statistical_ml", Direction.NEUTRAL, 40.0),
        _sig("sentiment", Direction.NEUTRAL, 40.0),
        _sig("macro", Direction.NEUTRAL, 40.0),
    ]:
        sid = tracker.insert_signal(sig)
        items.append((sid, sig))
    pm = PortfolioManager(tracker, md, cfg)
    plan = pm._score_ticker("AAA", items, tracker.get_weights(), equity=100.0, cash=100.0, positions={})
    cap = plan.snapshot.get("momentum_cap") or {}
    assert cap.get("momentum_capped") is False
    assert plan.score == pytest.approx(0.9 * 0.2)


def test_recompute_entry_score_applies_cap():
    signals = [
        {"agent_id": "momentum", "direction": "long", "confidence": 95},
        {"agent_id": "fundamental", "direction": "long", "confidence": 25},
        {"agent_id": "statistical_ml", "direction": "long", "confidence": 25},
        {"agent_id": "sentiment", "direction": "long", "confidence": 25},
        {"agent_id": "macro", "direction": "long", "confidence": 25},
    ]
    weights = {aid: 0.2 for aid in ("momentum", *OTHER_DESKS)}
    out = recompute_entry_score(signals, weights)
    assert out is not None
    expected_other = 4 * 0.05
    expected_mom = (0.40 / 0.60) * expected_other
    assert out["weighted_score"] == pytest.approx(round(expected_mom + expected_other, 6))
