"""Tracker DB + frozen schema + API tests."""

from __future__ import annotations

import json

import pytest

from quant_system.models.signal import Direction, Signal
from quant_system.models.ticket import HoldDecision, TicketSide, TradeTicket
from quant_system.tracker.api import Tracker
from quant_system.tracker.schema import SCHEMA_VERSION


@pytest.fixture()
def tracker(tmp_path):
    db = tmp_path / "test.db"
    t = Tracker(str(db))
    yield t
    t.close()


def test_schema_version_seeded(tracker: Tracker):
    row = tracker.conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    assert row is not None
    assert int(row["value"]) == SCHEMA_VERSION


def test_insert_and_list_signal(tracker: Tracker):
    sig = Signal(
        agent_id="momentum",
        ticker="ionq",
        direction=Direction.LONG,
        confidence=72.5,
        reasoning="MA20>MA50",
        supporting_data={"rsi": 58.0},
    )
    sid = tracker.insert_signal(sig)
    assert sid >= 1
    rows = tracker.list_signals(ticker="IONQ")
    assert len(rows) == 1
    assert rows[0]["ticker"] == "IONQ"
    assert rows[0]["direction"] == "long"
    assert rows[0]["supporting_data"]["rsi"] == 58.0
    decisions = tracker.list_decisions(decision_type="signal")
    assert any(d["payload"].get("signal_id") == sid for d in decisions)


def test_ticket_hold_fill_and_position(tracker: Tracker):
    tracker.set_state("cash", "100.0")
    ticket = TradeTicket(
        ticker="NVDA",
        side=TicketSide.BUY,
        qty=0.5,
        rationale="test buy",
        signal_ids=[1],
    )
    tid = tracker.insert_ticket(ticket)
    assert tid >= 1
    fill_id = tracker.insert_fill(tid, "NVDA", "buy", 0.5, 100.0, "dry_run")
    assert fill_id >= 1
    positions = tracker.list_positions()
    assert len(positions) == 1
    assert positions[0]["ticker"] == "NVDA"
    assert float(positions[0]["qty"]) == pytest.approx(0.5)
    cash = float(tracker.get_state("cash"))
    assert cash == pytest.approx(50.0)
    # sell
    sell = TradeTicket(ticker="NVDA", side=TicketSide.SELL, qty=0.5, rationale="exit")
    sid = tracker.insert_ticket(sell)
    tracker.insert_fill(sid, "NVDA", "sell", 0.5, 110.0, "dry_run")
    assert tracker.list_positions() == []
    assert float(tracker.get_state("cash")) == pytest.approx(105.0)


def test_halt_resume_blocks(tracker: Tracker):
    assert tracker.orders_blocked() == (False, "")
    tracker.halt("manual test")
    blocked, reason = tracker.orders_blocked()
    assert blocked is True
    assert "manual" in reason
    tracker.resume("ok")
    assert tracker.orders_blocked()[0] is False


def test_equal_weights_seeded(tracker: Tracker):
    w = tracker.get_weights()
    assert set(w) >= {"momentum", "fundamental", "statistical_ml", "sentiment", "macro"}
    assert abs(sum(w.values()) - 1.0) < 1e-9


def test_hold_and_digest(tracker: Tracker):
    tracker.insert_hold(HoldDecision(ticker="SPY", rationale="flat", signal_ids=[]))
    dig = tracker.digest(limit=5)
    assert "recent_decisions" in dig
    assert dig["db_path"]


def test_signal_model_forbids_extra():
    with pytest.raises(Exception):
        Signal(
            agent_id="x",
            ticker="A",
            direction=Direction.NEUTRAL,
            confidence=1,
            reasoning="r",
            supporting_data={},
            extra_field=True,  # type: ignore[call-arg]
        )
