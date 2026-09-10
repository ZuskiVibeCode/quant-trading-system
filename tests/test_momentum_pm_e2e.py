"""Momentum + PortfolioManager + dry-run E2E: signals -> ticket/hold -> sim fill."""

from __future__ import annotations

import pytest

from quant_system.config.loader import load_config
from quant_system.execution.execution_agent import ExecutionAgent
from quant_system.execution.portfolio_manager import PortfolioManager
from quant_system.market_data.yfinance_provider import MockMarketData
from quant_system.models.ticket import TradeTicket
from quant_system.pipeline import run_once
from quant_system.research.momentum import MomentumAgent
from quant_system.tracker.api import Tracker


@pytest.fixture()
def env(tmp_path):
    db = tmp_path / "e2e.db"
    tracker = Tracker(str(db))
    tracker.set_state("cash", "100.0")
    tracker.set_state("equity", "100.0")
    tracker.set_state("peak_equity", "100.0")
    tracker.set_state("day_start_equity", "100.0")
    # Cheap prices so $100 can buy fractional shares under 20% cap
    md = MockMarketData(
        prices={
            "IONQ": 10.0,
            "NVDA": 20.0,
            "SPY": 100.0,
            "RKLB": 8.0,
            "ISRG": 15.0,
        }
    )
    cfg = load_config()
    yield tracker, md, cfg
    tracker.close()


def test_momentum_emits_frozen_signal(env):
    tracker, md, _ = env
    agent = MomentumAgent(md)
    sig = agent.analyze("IONQ", benchmark="SPY")
    assert sig.agent_id == "momentum"
    assert sig.ticker == "IONQ"
    assert 0 <= sig.confidence <= 100
    assert sig.direction.value in ("long", "short", "neutral")
    assert "ma20" in sig.supporting_data or "raw_score" in sig.supporting_data
    assert "relative_volume" in sig.supporting_data
    assert "demand_imbalance" in sig.supporting_data
    assert "squeeze_on" in sig.supporting_data
    sid = tracker.insert_signal(sig)
    assert sid >= 1


def test_pm_never_invents_tickers(env):
    tracker, md, cfg = env
    pm = PortfolioManager(tracker, md, cfg)
    # Empty signals -> no tickets
    out = pm.weigh_and_decide([])
    assert out == []


def test_e2e_signal_to_fill_logging(env):
    tracker, md, cfg = env
    # Lower min confidence for mock determinism
    cfg.risk.min_confidence_to_trade = 1.0
    agent = MomentumAgent(md)
    sig = agent.analyze("IONQ", benchmark="SPY")
    # Force a long signal for deterministic ticket path
    from quant_system.models.signal import Direction, Signal

    forced = Signal(
        agent_id="momentum",
        ticker="IONQ",
        direction=Direction.LONG,
        confidence=90.0,
        reasoning="forced long for e2e",
        supporting_data={"test": True},
    )
    sid = tracker.insert_signal(forced)
    pm = PortfolioManager(tracker, md, cfg)
    decisions = pm.weigh_and_decide([(sid, forced)])
    assert decisions
    tickets = [d for d in decisions if isinstance(d, TradeTicket)]
    assert tickets, f"expected ticket, got {decisions}"
    import os

    for k in (
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "ALPACA_API_SECRET",
    ):
        os.environ.pop(k, None)
    ex = ExecutionAgent(tracker, md)
    assert ex.mode == "dry_run"
    result = ex.execute(tickets[0])
    assert result is not None
    assert result["mode"] == "dry_run"
    fills = tracker.list_fills()
    assert len(fills) >= 1
    decisions_log = tracker.list_decisions()
    types = {d["decision_type"] for d in decisions_log}
    assert "signal" in types
    assert "ticket" in types
    assert "fill" in types


def test_halt_blocks_orders(env):
    tracker, md, cfg = env
    cfg.risk.min_confidence_to_trade = 1.0
    from quant_system.models.signal import Direction, Signal

    sig = Signal(
        agent_id="momentum",
        ticker="IONQ",
        direction=Direction.LONG,
        confidence=90.0,
        reasoning="should block",
        supporting_data={},
    )
    sid = tracker.insert_signal(sig)
    tracker.halt("test halt")
    pm = PortfolioManager(tracker, md, cfg)
    decisions = pm.weigh_and_decide([(sid, sig)])
    assert all(not isinstance(d, TradeTicket) for d in decisions)
    # Direct execution path also blocks
    ticket = TradeTicket(ticker="IONQ", side="buy", qty=1.0, rationale="x")
    ticket.id = tracker.insert_ticket(ticket)
    ex = ExecutionAgent(tracker, md)
    assert ex.execute(ticket) is None
    assert tracker.get_ticket(ticket.id)["status"] == "blocked"


def test_run_once_mock_writes_rows(tmp_path):
    db = tmp_path / "run.db"
    tracker = Tracker(str(db))
    tracker.set_state("cash", "100.0")
    tracker.set_state("equity", "100.0")
    tracker.set_state("peak_equity", "100.0")
    tracker.set_state("day_start_equity", "100.0")
    summary = run_once(
        tracker=tracker,
        mock=True,
        tickers=["IONQ", "NVDA"],
    )
    assert summary["signals"] >= 2 * 5  # 2 tickers x 5 agents
    assert tracker.list_signals(limit=200)
    assert tracker.list_decisions(limit=200)
    dig = tracker.digest()
    assert dig["recent_signals"]
    tracker.close()
