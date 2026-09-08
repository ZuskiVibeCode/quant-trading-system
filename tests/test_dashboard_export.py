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


def test_closed_and_holdings_reasoning(tmp_path):
    """Sell fills become closed legs with linked ticket/signal reasoning; holdings get buy bundle."""
    db = str(tmp_path / "reason.db")
    docs = tmp_path / "docs"
    tracker = Tracker(db)
    tracker.set_state("cash", "100.0")
    tracker.set_state("equity", "100.0")
    from quant_system.models.signal import Direction
    from quant_system.models.ticket import TradeTicket, TicketSide

    buy_sigs = []
    for agent, direction, conf, why in [
        ("momentum", Direction.LONG, 70.0, "buy momentum thesis"),
        ("macro", Direction.LONG, 80.0, "buy macro thesis"),
    ]:
        buy_sigs.append(
            tracker.insert_signal(
                Signal(
                    agent_id=agent,
                    ticker="RGTI",
                    direction=direction,
                    confidence=conf,
                    reasoning=why,
                    supporting_data={"theme": "quantum"},
                )
            )
        )
    buy_tid = tracker.insert_ticket(
        TradeTicket(
            ticker="RGTI",
            side=TicketSide.BUY,
            qty=1.0,
            rationale="BUY 1 RGTI for test",
            signal_ids=buy_sigs,
        )
    )
    tracker.insert_fill(ticket_id=buy_tid, ticker="RGTI", side="buy", qty=1.0, price=10.0, mode="dry_run")

    sell_sigs = []
    for agent, direction, conf, why in [
        ("statistical_ml", Direction.SHORT, 85.0, "sell stat thesis"),
        ("sentiment", Direction.NEUTRAL, 40.0, "sell sentiment thesis"),
    ]:
        sell_sigs.append(
            tracker.insert_signal(
                Signal(
                    agent_id=agent,
                    ticker="RGTI",
                    direction=direction,
                    confidence=conf,
                    reasoning=why,
                    supporting_data={"edge_type": "mean_reversion"},
                )
            )
        )
    sell_tid = tracker.insert_ticket(
        TradeTicket(
            ticker="RGTI",
            side=TicketSide.SELL,
            qty=0.25,
            rationale="SELL 0.25 RGTI reduce risk",
            signal_ids=sell_sigs,
        )
    )
    tracker.insert_fill(ticket_id=sell_tid, ticker="RGTI", side="sell", qty=0.25, price=12.0, mode="dry_run")
    tracker.close()

    assert main(["--db", db, "export-dashboard", "--docs", str(docs), "--no-alpaca", "--no-history"]) == 0
    payload = json.loads((docs / "data" / "latest.json").read_text())
    assert validate_payload(payload) == []

    assert len(payload["closed"]) == 1
    c = payload["closed"][0]
    assert c["ticker"] == "RGTI"
    assert c["qty"] == 0.25
    assert c["entry"] == 10.0
    assert c["exit"] == 12.0
    assert abs(c["pnl"] - 0.5) < 1e-9
    assert c["sell"]["ticket"]["rationale"].startswith("SELL")
    assert {s["reasoning"] for s in c["sell"]["signals"]} >= {"sell stat thesis", "sell sentiment thesis"}
    assert c["buy"]["ticket"]["rationale"].startswith("BUY")
    assert {s["agent_id"] for s in c["buy"]["signals"]} >= {"momentum", "macro"}
    assert c["sell"]["fill"]["price"] == 12.0
    assert c["buy"]["fill"]["price"] == 10.0

    assert len(payload["holdings"]) == 1
    h = payload["holdings"][0]
    assert h["ticker"] == "RGTI"
    assert h["buy"]["ticket"]["rationale"].startswith("BUY")
    assert len(h["buy"]["signals"]) >= 2
    assert isinstance(h["fills"], list) and len(h["fills"]) == 2
