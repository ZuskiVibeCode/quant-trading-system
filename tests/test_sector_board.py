"""Unit tests for Orbit sector_board builder (pressure / breadth / book)."""

from __future__ import annotations

import json
from pathlib import Path

from quant_system.dashboard_export import (
    build_sector_board,
    export_dashboard,
    validate_payload,
)
from quant_system.models.signal import Direction, Signal
from quant_system.tracker.api import Tracker


def _agents_fixture() -> dict:
    """Two themes with known pressures; robotics empty of signals."""
    return {
        "momentum": {
            "agent_id": "momentum",
            "signals": [
                # newest first
                {"ticker": "IONQ", "direction": "long", "confidence": 80.0},
                {"ticker": "RKLB", "direction": "short", "confidence": 60.0},
            ],
        },
        "macro": {
            "agent_id": "macro",
            "signals": [
                {"ticker": "IONQ", "direction": "long", "confidence": 40.0},
                {"ticker": "RKLB", "direction": "short", "confidence": 40.0},
            ],
        },
        "fundamental": {
            "agent_id": "fundamental",
            "signals": [
                {"ticker": "IONQ", "direction": "neutral", "confidence": 50.0},
                {"ticker": "RKLB", "direction": "long", "confidence": 20.0},
            ],
        },
    }


SECTORS = {
    "quantum": ["IONQ", "RGTI"],  # RGTI has no signals → excluded from pressure mean
    "space": ["RKLB"],
    "robotics": ["PATH", "NVDA"],
}


HOLDINGS = [
    {"ticker": "IONQ", "qty": 2.0, "avg_cost": 10.0, "last": 12.0, "unrealized": 4.0, "sector": "quantum"},
    {"ticker": "PATH", "qty": 1.0, "avg_cost": 20.0, "last": 18.0, "unrealized": -2.0, "sector": "robotics"},
]


def test_build_sector_board_pressure_breadth_book():
    board = build_sector_board(_agents_fixture(), SECTORS, HOLDINGS, equity=100.0)
    by = {r["theme"]: r for r in board}
    assert set(by) == {"quantum", "robotics", "space"}

    q = by["quantum"]
    # IONQ only (RGTI silent): buy=(0.8+0.4+0)/3=0.4, sell=0, net=0.4
    assert abs(q["buy"] - 0.4) < 1e-9
    assert abs(q["sell"] - 0.0) < 1e-9
    assert abs(q["net"] - 0.4) < 1e-9
    assert q["signaled_n"] == 1
    assert q["tickers_n"] == 2
    assert q["held_n"] == 1
    assert q["long"] == 1 and q["short"] == 0 and q["neutral"] == 0
    assert q["zones"]["buy_zone"] == 1
    # weight = |2*12| / 100 = 0.24
    assert abs(q["weight"] - 0.24) < 1e-9
    assert abs(q["unrealized"] - 4.0) < 1e-9

    s = by["space"]
    # RKLB: buy=(0+0+0.2)/3 ≈ 0.066667, sell=(0.6+0.4+0)/3 ≈ 0.333333 (rounded to 6 dp)
    assert abs(s["buy"] - round(0.2 / 3, 6)) < 1e-12
    assert abs(s["sell"] - round(1.0 / 3, 6)) < 1e-12
    assert s["long"] == 0 and s["short"] == 1  # shorts majority (2 vs 1)
    assert s["held_n"] == 0
    assert s["weight"] == 0.0
    assert s["unrealized"] is None

    r = by["robotics"]
    assert r["buy"] == 0.0 and r["sell"] == 0.0 and r["net"] == 0.0
    assert r["signaled_n"] == 0
    assert r["held_n"] == 1
    assert r["tickers_n"] == 2
    # PATH mv = 18 / 100
    assert abs(r["weight"] - 0.18) < 1e-9
    assert abs(r["unrealized"] - (-2.0)) < 1e-9
    assert r["zones"] == {"buy_zone": 0, "sell_zone": 0, "conflicted": 0, "quiet": 0}


def test_build_sector_board_empty_inputs():
    assert build_sector_board({}, {}, [], equity=100.0) == []
    board = build_sector_board({}, {"quantum": ["IONQ"]}, [], equity=0.0)
    assert len(board) == 1
    assert board[0]["theme"] == "quantum"
    assert board[0]["weight"] == 0.0


def test_export_includes_sector_board(tmp_path):
    db = str(tmp_path / "sectors.db")
    docs = tmp_path / "docs"
    tracker = Tracker(db)
    tracker.reconcile_portfolio(
        cash=90.0,
        equity=100.0,
        positions=[{"ticker": "IONQ", "qty": 1.0, "avg_cost": 10.0}],
        source="test",
    )
    tracker.insert_signal(
        Signal(
            agent_id="momentum",
            ticker="IONQ",
            direction=Direction.LONG,
            confidence=80.0,
            reasoning="sector board test",
            supporting_data={},
        )
    )
    tracker.close()

    result = export_dashboard(
        Tracker(db),
        docs_dir=docs,
        include_alpaca=False,
        close_tracker=True,
    )
    payload = json.loads(Path(result["latest"]).read_text())
    assert validate_payload(payload) == []
    assert "sector_board" in payload
    themes = {r["theme"] for r in payload["sector_board"]}
    assert "quantum" in themes
    ionq_theme = next(r for r in payload["sector_board"] if r["theme"] == "quantum")
    assert ionq_theme["buy"] > 0
    assert "zones" in ionq_theme
    blob = json.dumps(payload)
    assert "ALPACA_SECRET" not in blob
    assert "secret_key" not in blob
