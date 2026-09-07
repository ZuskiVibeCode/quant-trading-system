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
        assert aid in payload["agents_history"]
        assert isinstance(payload["agents_history"][aid], list)
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
