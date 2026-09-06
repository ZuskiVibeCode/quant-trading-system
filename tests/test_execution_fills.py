"""ExecutionAgent fill semantics: ACCEPTED is not a fill; API errors do not crash."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from quant_system.execution.execution_agent import ExecutionAgent, is_real_fill
from quant_system.market_data.yfinance_provider import MockMarketData
from quant_system.models.ticket import TicketSide, TicketStatus, TradeTicket
from quant_system.tracker.api import Tracker


@pytest.fixture()
def tracker(tmp_path):
    t = Tracker(str(tmp_path / "exec.db"))
    t.set_state("cash", "100.0")
    t.set_state("equity", "100.0")
    t.set_state("peak_equity", "100.0")
    t.set_state("day_start_equity", "100.0")
    yield t
    t.close()


def _ticket(tracker: Tracker, ticker: str = "IONQ", qty: float = 1.0) -> TradeTicket:
    ticket = TradeTicket(
        ticker=ticker,
        side=TicketSide.BUY,
        qty=qty,
        rationale="test",
        signal_ids=[1],
    )
    ticket.id = tracker.insert_ticket(ticket)
    return ticket


def test_is_real_fill_accepted_zero_qty():
    order = SimpleNamespace(status="accepted", filled_qty=0, filled_avg_price=None)
    assert is_real_fill(order) is False


def test_is_real_fill_filled():
    order = SimpleNamespace(status="filled", filled_qty="2", filled_avg_price="10.5")
    assert is_real_fill(order) is True


def test_accepted_order_does_not_record_fill_or_move_cash(tracker: Tracker, monkeypatch):
    monkeypatch.setenv("QUANT_ALLOW_ALPACA_IN_TESTS", "1")
    monkeypatch.setenv("ALPACA_API_KEY", "PK_TEST")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SK_TEST")
    monkeypatch.setenv("ALPACA_PAPER", "true")

    md = MockMarketData(prices={"IONQ": 10.0})
    agent = ExecutionAgent(tracker, md)
    mock_client = MagicMock()
    mock_client.submit_order.return_value = SimpleNamespace(
        id="ord-accepted-1",
        status="accepted",
        filled_qty=0,
        filled_avg_price=None,
    )
    agent._alpaca = mock_client
    agent.creds = {"api_key": "PK_TEST", "secret_key": "SK_TEST", "paper": True, "configured": True}

    ticket = _ticket(tracker)
    cash_before = float(tracker.get_state("cash"))
    result = agent.execute(ticket)

    assert result is not None
    assert result.get("status") == "accepted"
    assert "fill_id" not in result
    assert tracker.list_fills() == []
    assert tracker.list_positions() == []
    assert float(tracker.get_state("cash")) == pytest.approx(cash_before)
    assert tracker.get_ticket(ticket.id)["status"] == TicketStatus.SUBMITTED.value
    decisions = tracker.list_decisions(decision_type="order_submitted")
    assert decisions
    assert decisions[0]["payload"]["status"] == "accepted"


def test_api_error_logs_and_returns_without_raising(tracker: Tracker, monkeypatch):
    monkeypatch.setenv("QUANT_ALLOW_ALPACA_IN_TESTS", "1")
    monkeypatch.setenv("ALPACA_API_KEY", "PK_TEST")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SK_TEST")
    monkeypatch.setenv("ALPACA_PAPER", "true")

    md = MockMarketData(prices={"IONQ": 10.0})
    agent = ExecutionAgent(tracker, md)
    mock_client = MagicMock()
    mock_client.submit_order.side_effect = Exception(
        '{"message": "insufficient buying power"}'
    )
    agent._alpaca = mock_client
    agent.creds = {"api_key": "PK_TEST", "secret_key": "SK_TEST", "paper": True, "configured": True}

    ticket = _ticket(tracker)
    cash_before = float(tracker.get_state("cash"))
    result = agent.execute(ticket)

    assert result is not None
    assert result["status"] == "failed"
    assert "insufficient buying power" in result["error"]
    assert tracker.list_fills() == []
    assert float(tracker.get_state("cash")) == pytest.approx(cash_before)
    assert tracker.get_ticket(ticket.id)["status"] == TicketStatus.FAILED.value
    failed = tracker.list_decisions(decision_type="order_failed")
    assert failed
    assert "insufficient" in failed[0]["reasoning"].lower() or "insufficient" in str(failed[0]["payload"]).lower()


def test_pipeline_continues_after_execution_exception(tracker: Tracker, monkeypatch):
    """Safety net: uncaught execute() exception must not abort run-once."""
    from quant_system.config.loader import load_config
    from quant_system.pipeline import run_once

    class BoomAgent(ExecutionAgent):
        def execute(self, ticket):
            raise RuntimeError("403 insufficient buying power")

    monkeypatch.setattr(
        "quant_system.pipeline.ExecutionAgent",
        BoomAgent,
    )
    cfg = load_config()
    cfg.risk.min_confidence_to_trade = 1.0

    # Force a ticket via PM by injecting a long signal path through mock prices
    # Use run_once with mock; even if no tickets, ensure no crash when BoomAgent used.
    # Seed a ticket path: monkeypatch PortfolioManager to return a ticket.
    from quant_system.models.ticket import TradeTicket, TicketSide

    ticket = TradeTicket(ticker="IONQ", side=TicketSide.BUY, qty=1.0, rationale="forced")
    ticket.id = tracker.insert_ticket(ticket)

    def fake_decide(self, stored):
        return [ticket]

    monkeypatch.setattr(
        "quant_system.pipeline.PortfolioManager.weigh_and_decide",
        fake_decide,
    )
    summary = run_once(
        tracker=tracker,
        config=cfg,
        tickers=["IONQ"],
        mock=True,
    )
    assert "errors" in summary
    assert any("insufficient buying power" in e for e in summary["errors"])
    assert summary["cash"] == pytest.approx(100.0)


def test_reconcile_portfolio_clears_phantom_positions(tracker: Tracker):
    tracker.set_state("cash", "10.0")
    tracker.set_state("equity", "50.0")
    tracker._conn.execute(
        "INSERT INTO positions(ticker, qty, avg_cost, updated_at) VALUES (?,?,?,?)",
        ("PHANTOM", 3.0, 10.0, "2026-01-01T00:00:00Z"),
    )
    tracker._conn.commit()
    assert len(tracker.list_positions()) == 1

    out = tracker.reconcile_portfolio(
        cash=100.0,
        equity=100.0,
        positions=[],
        source="alpaca",
    )
    assert out["positions_cleared"] == 1
    assert tracker.list_positions() == []
    assert float(tracker.get_state("cash")) == pytest.approx(100.0)
    assert float(tracker.get_state("equity")) == pytest.approx(100.0)
    assert tracker.list_decisions(decision_type="reconcile")


def test_reconcile_portfolio_writes_broker_positions(tracker: Tracker):
    out = tracker.reconcile_portfolio(
        cash=80.0,
        equity=100.0,
        positions=[{"ticker": "NVDA", "qty": 0.5, "avg_cost": 40.0}],
        source="alpaca",
    )
    assert out["positions_written"] == 1
    pos = tracker.list_positions()
    assert len(pos) == 1
    assert pos[0]["ticker"] == "NVDA"
    assert float(pos[0]["qty"]) == pytest.approx(0.5)
    assert float(tracker.get_state("cash")) == pytest.approx(80.0)
