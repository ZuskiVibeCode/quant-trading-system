"""Offline tests for Alpaca account snapshot mark fields."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from quant_system.execution.broker import fetch_account_snapshot


def test_fetch_account_snapshot_copies_mark_fields():
    """Position market_value / current_price / unrealized_pl are mapped (floats)."""
    pos = SimpleNamespace(
        symbol="IONQ",
        qty="10",
        avg_entry_price="20.5",
        market_value="215.0",
        current_price="21.5",
        unrealized_pl="10.0",
        unrealized_plpc="0.0488",
    )
    account = SimpleNamespace(cash="1000", equity="1215", buying_power="2000")
    client = MagicMock()
    client.get_account.return_value = account
    client.get_all_positions.return_value = [pos]
    client.get_orders.return_value = []

    snap = fetch_account_snapshot(client)

    assert snap["cash"] == 1000.0
    assert snap["equity"] == 1215.0
    assert len(snap["positions"]) == 1
    p = snap["positions"][0]
    assert p["ticker"] == "IONQ"
    assert p["qty"] == 10.0
    assert p["avg_cost"] == 20.5
    assert p["market_value"] == 215.0
    assert p["current_price"] == 21.5
    assert p["unrealized_pl"] == 10.0


def test_fetch_account_snapshot_missing_marks_are_none():
    """Missing mark attributes stay None without breaking ticker/qty/avg_cost."""
    pos = SimpleNamespace(symbol="RKLB", qty=2, avg_entry_price=8.0)
    account = SimpleNamespace(cash=50, equity=66, buying_power=50)
    client = MagicMock()
    client.get_account.return_value = account
    client.get_all_positions.return_value = [pos]
    client.get_orders.return_value = []

    p = fetch_account_snapshot(client)["positions"][0]
    assert p["ticker"] == "RKLB"
    assert p["qty"] == 2.0
    assert p["avg_cost"] == 8.0
    assert p["market_value"] is None
    assert p["current_price"] is None
    assert p["unrealized_pl"] is None
