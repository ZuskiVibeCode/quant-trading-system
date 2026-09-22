"""Minimal schema tests for export-dashboard."""

from __future__ import annotations

import json
from pathlib import Path

from quant_system.cli import main
from quant_system.dashboard_export import (
    REQUIRED_TOP_KEYS,
    build_dashboard_payload,
    export_dashboard,
    validate_payload,
)
from quant_system.models.signal import Signal
from quant_system.tracker.api import Tracker
from quant_system.tracker.schema import DEFAULT_AGENT_IDS


def test_export_schema_and_files(tmp_path, capsys):
    db = str(tmp_path / "dash.db")
    docs = tmp_path / "docs"
    tracker = Tracker(db)
    tracker.snapshot_portfolio(equity=100.0, cash=100.0, positions=[], daily_pnl=0.0, drawdown=0.0, peak_equity=100.0)
    from quant_system.models.signal import Direction

    tracker.insert_signal(
        Signal(
            agent_id="momentum",
            ticker="IONQ",
            direction=Direction.LONG,
            confidence=70.0,
            reasoning="test momentum long",
            supporting_data={"rsi": 55.0, "theme": "quantum"},
        )
    )
    tracker.close()

    rc = main(["--db", db, "export-dashboard", "--docs", str(docs), "--no-alpaca"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert Path(out["latest"]).is_file()

    payload = json.loads(Path(out["latest"]).read_text())
    assert validate_payload(payload) == []
    for k in REQUIRED_TOP_KEYS:
        assert k in payload
    assert payload["meta"]["paper"] is True
    assert payload["agents_today"]["momentum"]["count"] >= 1
    sig = payload["agents_today"]["momentum"]["signals"][0]
    assert sig["ticker"] == "IONQ"
    assert "reasoning" in sig
    assert "supporting_data" in sig
    for aid in DEFAULT_AGENT_IDS:
        assert aid in payload["agents_today"]
    blob = json.dumps(payload)
    assert "ALPACA_SECRET" not in blob
    assert "secret_key" not in blob


def test_build_payload_agents_shape(tmp_path):
    db = str(tmp_path / "dash2.db")
    tracker = Tracker(db)
    payload = build_dashboard_payload(tracker, include_alpaca=False)
    errs = validate_payload(payload)
    assert errs == []
    assert isinstance(payload["watchlist"], list)
    assert "quantum" in payload["sectors"]
    tracker.close()


def test_holdings_enrich_from_alpaca_marks(tmp_path, monkeypatch):
    """Holdings last/unrealized come from Alpaca current_price + unrealized_pl dollars."""
    from quant_system import dashboard_export as de

    fake_alpaca = {
        "cash": 50.0,
        "equity": 265.0,
        "buying_power": 50.0,
        "open_orders_count": 0,
        "paper": True,
        "positions": [
            {
                "ticker": "IONQ",
                "qty": 10.0,
                "avg_cost": 20.0,
                "market_value": 220.0,
                "current_price": 22.0,
                "unrealized_pl": 20.0,
            }
        ],
    }
    monkeypatch.setattr(de, "_optional_alpaca_snapshot", lambda: fake_alpaca)

    db = str(tmp_path / "enrich.db")
    tracker = Tracker(db)
    tracker.reconcile_portfolio(
        cash=50.0,
        equity=265.0,
        positions=[{"ticker": "IONQ", "qty": 10.0, "avg_cost": 20.0}],
        source="test",
    )
    payload = build_dashboard_payload(tracker, include_alpaca=True)
    tracker.close()

    assert len(payload["holdings"]) == 1
    h = payload["holdings"][0]
    assert h["ticker"] == "IONQ"
    assert h["last"] == 22.0
    assert h["unrealized"] == 20.0


def test_holdings_enrich_falls_back_to_market_value_over_qty(tmp_path, monkeypatch):
    """When current_price is absent, last = market_value / qty."""
    from quant_system import dashboard_export as de

    fake_alpaca = {
        "cash": 0.0,
        "equity": 100.0,
        "buying_power": 0.0,
        "open_orders_count": 0,
        "paper": True,
        "positions": [
            {
                "ticker": "ASTS",
                "qty": 4.0,
                "avg_cost": 20.0,
                "market_value": 100.0,
                "current_price": None,
                "unrealized_pl": 20.0,
            }
        ],
    }
    monkeypatch.setattr(de, "_optional_alpaca_snapshot", lambda: fake_alpaca)

    db = str(tmp_path / "enrich2.db")
    tracker = Tracker(db)
    tracker.reconcile_portfolio(
        cash=0.0,
        equity=100.0,
        positions=[{"ticker": "ASTS", "qty": 4.0, "avg_cost": 20.0}],
        source="test",
    )
    payload = build_dashboard_payload(tracker, include_alpaca=True)
    tracker.close()

    h = payload["holdings"][0]
    assert h["last"] == 25.0
    assert h["unrealized"] == 20.0
