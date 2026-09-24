"""Unit tests for Orbit lifetime ticker_pnl builder."""

from __future__ import annotations

import pytest

from quant_system.dashboard_export import build_ticker_pnl, _build_closed
from quant_system.models.ticket import TicketSide, TicketStatus, TradeTicket
from quant_system.tracker.api import Tracker


def _fill(tracker: Tracker, ticker: str, side: str, qty: float, price: float, at: str, mode: str = "alpaca") -> None:
    t = TradeTicket(
        ticker=ticker,
        side=TicketSide.BUY if side == "buy" else TicketSide.SELL,
        qty=qty,
        rationale=f"test {side}",
        status=TicketStatus.FILLED,
        created_at=at,
    )
    tid = tracker.insert_ticket(t)
    tracker.insert_fill_historical(
        tid, ticker, side, qty, price, mode, at, apply_to_position=False
    )


def test_multi_round_buy_sell_buy_sell(tmp_path):
    """buy, sell, buy, sell → realized sum, series length 2, cum correct."""
    db = str(tmp_path / "multi.db")
    tracker = Tracker(db)
    _fill(tracker, "IONQ", "buy", 2.0, 10.0, "2026-09-01T10:00:00.000000Z")
    _fill(tracker, "IONQ", "sell", 2.0, 12.0, "2026-09-02T10:00:00.000000Z")  # +4
    _fill(tracker, "IONQ", "buy", 1.0, 11.0, "2026-09-03T10:00:00.000000Z")
    _fill(tracker, "IONQ", "sell", 1.0, 10.0, "2026-09-04T10:00:00.000000Z")  # -1

    rows = build_ticker_pnl(tracker, {"IONQ": "quantum"}, holdings=[])
    tracker.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "IONQ"
    assert row["sector"] == "quantum"
    assert row["realized"] == pytest.approx(3.0)
    assert row["unrealized"] is None
    assert row["total"] == pytest.approx(3.0)
    assert row["close_events"] == 2
    assert row["buy_events"] == 2
    assert row["qty_open"] == pytest.approx(0.0)
    assert len(row["series"]) == 2
    assert row["series"][0]["pnl"] == pytest.approx(4.0)
    assert row["series"][0]["cum"] == pytest.approx(4.0)
    assert row["series"][1]["pnl"] == pytest.approx(-1.0)
    assert row["series"][1]["cum"] == pytest.approx(3.0)


def test_partial_trim_still_open(tmp_path):
    """buy 2, sell 1 → realized for trim + residual qty + unrealized from holdings."""
    db = str(tmp_path / "trim.db")
    tracker = Tracker(db)
    _fill(tracker, "ASTS", "buy", 2.0, 20.0, "2026-09-01T10:00:00.000000Z")
    _fill(tracker, "ASTS", "sell", 1.0, 25.0, "2026-09-02T10:00:00.000000Z")  # +5

    holdings = [
        {
            "ticker": "ASTS",
            "qty": 1.0,
            "avg_cost": 20.0,
            "unrealized": 3.5,
            "sector": "space",
        }
    ]
    rows = build_ticker_pnl(tracker, {"ASTS": "space"}, holdings=holdings)
    tracker.close()

    row = rows[0]
    assert row["realized"] == pytest.approx(5.0)
    assert row["unrealized"] == pytest.approx(3.5)
    assert row["total"] == pytest.approx(8.5)
    assert row["qty_open"] == pytest.approx(1.0)
    assert row["avg_cost_open"] == pytest.approx(20.0)
    assert row["close_events"] == 1
    assert len(row["series"]) == 1


def test_sell_without_prior_buy(tmp_path):
    """Sell without prior buy → pnl null, not in series; ticker still appears."""
    db = str(tmp_path / "orphan.db")
    tracker = Tracker(db)
    _fill(tracker, "LUNR", "sell", 0.2, 13.99, "2026-09-18T14:04:52.000000Z")

    rows = build_ticker_pnl(tracker, {"LUNR": "space"}, holdings=[])
    tracker.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "LUNR"
    assert row["realized"] is None
    assert row["close_events"] == 0
    assert row["buy_events"] == 0
    assert row["series"] == []
    assert row["qty_open"] == pytest.approx(0.0)


def test_alpaca_backfill_buy_then_sell(tmp_path):
    """Backfilled buy (later id, earlier created_at) supplies entry for sell."""
    db = str(tmp_path / "bf.db")
    tracker = Tracker(db)
    # Sell logged first
    _fill(tracker, "IONQ", "sell", 0.1, 39.0, "2026-09-17T13:52:37.000000Z", mode="alpaca")
    # Backfill buy with earlier created_at
    _fill(
        tracker,
        "IONQ",
        "buy",
        0.5,
        42.29,
        "2026-09-08T13:30:02.000000Z",
        mode="alpaca_backfill",
    )

    closed = _build_closed(tracker, {"IONQ": "quantum"})
    rows = build_ticker_pnl(tracker, {"IONQ": "quantum"}, holdings=[])
    tracker.close()

    assert closed[0]["pnl"] == pytest.approx((39.0 - 42.29) * 0.1)
    row = rows[0]
    assert row["realized"] == pytest.approx((39.0 - 42.29) * 0.1)
    assert row["close_events"] == 1
    assert row["buy_events"] == 1
    assert len(row["series"]) == 1
    assert row["qty_open"] == pytest.approx(0.4)


def test_sort_by_last_activity_desc(tmp_path):
    db = str(tmp_path / "sort.db")
    tracker = Tracker(db)
    _fill(tracker, "AAA", "buy", 1.0, 10.0, "2026-09-01T10:00:00.000000Z")
    _fill(tracker, "BBB", "buy", 1.0, 10.0, "2026-09-05T10:00:00.000000Z")
    rows = build_ticker_pnl(tracker, {}, holdings=[])
    tracker.close()
    assert [r["ticker"] for r in rows] == ["BBB", "AAA"]


def test_buys_only_realized_zero(tmp_path):
    db = str(tmp_path / "open.db")
    tracker = Tracker(db)
    _fill(tracker, "RGTI", "buy", 1.0, 15.0, "2026-09-01T10:00:00.000000Z")
    holdings = [{"ticker": "RGTI", "qty": 1.0, "avg_cost": 15.0, "unrealized": 2.0}]
    rows = build_ticker_pnl(tracker, {"RGTI": "quantum"}, holdings=holdings)
    tracker.close()
    row = rows[0]
    assert row["realized"] == 0.0
    assert row["unrealized"] == pytest.approx(2.0)
    assert row["total"] == pytest.approx(2.0)
    assert row["close_events"] == 0
    assert row["series"] == []
