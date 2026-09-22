"""Unit tests for Orbit buy/sell zone pressure aggregation and export."""

from __future__ import annotations

import json
from pathlib import Path

from quant_system.dashboard_export import (
    BUY_ZONE_MIN,
    SELL_ZONE_MIN,
    build_zone_points,
    build_zones_block,
    compute_zone_pressures,
    export_dashboard,
    rebuild_zones_history,
    validate_payload,
    zone_label,
)
from quant_system.models.signal import Direction, Signal
from quant_system.tracker.api import Tracker


def test_compute_zone_pressures_long_short_neutral():
    agents = {
        "momentum": {"direction": "long", "confidence": 80.0},
        "fundamental": {"direction": "short", "confidence": 40.0},
        "macro": {"direction": "neutral", "confidence": 50.0},
        "sentiment": {"direction": "long", "confidence": 60.0},
        "statistical_ml": {"direction": "short", "confidence": 20.0},
    }
    buy, sell, net = compute_zone_pressures(agents)
    # buy = (0.8 + 0 + 0 + 0.6 + 0) / 5 = 0.28
    # sell = (0 + 0.4 + 0 + 0 + 0.2) / 5 = 0.12
    assert abs(buy - 0.28) < 1e-9
    assert abs(sell - 0.12) < 1e-9
    assert abs(net - 0.16) < 1e-9


def test_compute_zone_pressures_empty():
    assert compute_zone_pressures({}) == (0.0, 0.0, 0.0)


def test_zone_label_thresholds():
    assert zone_label(0.40, 0.10) == "buy_zone"
    assert zone_label(0.10, 0.40) == "sell_zone"
    assert zone_label(0.40, 0.40) == "conflicted"
    assert zone_label(0.20, 0.20) == "quiet"
    # boundary: sell must be strictly below quiet max for buy_zone
    assert zone_label(BUY_ZONE_MIN, SELL_ZONE_MIN) == "conflicted"
    assert zone_label(0.35, 0.24) == "buy_zone"


def test_latest_signal_per_agent_used():
    """Newer signal wins when an agent fires twice the same day."""
    agents_today = {
        "momentum": {
            "agent_id": "momentum",
            "signals": [
                # newest first
                {"ticker": "IONQ", "direction": "long", "confidence": 90.0, "created_at": "2026-09-22T18:00:00Z"},
                {"ticker": "IONQ", "direction": "short", "confidence": 99.0, "created_at": "2026-09-22T12:00:00Z"},
            ],
        },
        "macro": {
            "agent_id": "macro",
            "signals": [
                {"ticker": "IONQ", "direction": "long", "confidence": 50.0, "created_at": "2026-09-22T18:00:00Z"},
            ],
        },
    }
    points = build_zone_points(agents_today, held={"IONQ"}, compact=False)
    assert len(points) == 1
    pt = points[0]
    assert pt["ticker"] == "IONQ"
    assert pt["held"] is True
    # buy = (0.9 + 0.5) / 2 = 0.7; sell = 0 (old short discarded)
    assert abs(pt["buy"] - 0.7) < 1e-9
    assert abs(pt["sell"] - 0.0) < 1e-9
    assert pt["zone"] == "buy_zone"
    dirs = {a["agent_id"]: a["direction"] for a in pt["agents"]}
    assert dirs["momentum"] == "long"


def test_rebuild_zones_history_from_files(tmp_path):
    hist = tmp_path / "history"
    hist.mkdir()
    day_a = {
        "meta": {"day_local": "2026-09-01"},
        "holdings": [{"ticker": "IBM"}],
        "agents_today": {
            "momentum": {
                "agent_id": "momentum",
                "signals": [{"ticker": "IBM", "direction": "long", "confidence": 80}],
            },
            "macro": {
                "agent_id": "macro",
                "signals": [{"ticker": "IBM", "direction": "long", "confidence": 80}],
            },
        },
    }
    day_b = {
        "meta": {"day_local": "2026-09-02"},
        "holdings": [],
        "agents_today": {
            "momentum": {
                "agent_id": "momentum",
                "signals": [{"ticker": "PATH", "direction": "short", "confidence": 90}],
            },
            "macro": {
                "agent_id": "macro",
                "signals": [{"ticker": "PATH", "direction": "short", "confidence": 90}],
            },
        },
    }
    (hist / "2026-09-01.json").write_text(json.dumps(day_a))
    (hist / "2026-09-02.json").write_text(json.dumps(day_b))
    out = rebuild_zones_history(hist)
    assert [d["day"] for d in out["days"]] == ["2026-09-01", "2026-09-02"]
    assert out["days"][0]["points"][0]["ticker"] == "IBM"
    assert out["days"][0]["points"][0]["held"] is True
    assert out["days"][0]["points"][0]["zone"] == "buy_zone"
    assert out["days"][1]["points"][0]["zone"] == "sell_zone"


def test_export_includes_zones_without_secrets(tmp_path, capsys):
    db = str(tmp_path / "zones.db")
    docs = tmp_path / "docs"
    tracker = Tracker(db)
    tracker.snapshot_portfolio(
        equity=100.0, cash=100.0, positions=[], daily_pnl=0.0, drawdown=0.0, peak_equity=100.0
    )
    tracker.insert_signal(
        Signal(
            agent_id="momentum",
            ticker="IONQ",
            direction=Direction.LONG,
            confidence=80.0,
            reasoning="zone test long",
            supporting_data={"rsi": 55.0},
        )
    )
    tracker.insert_signal(
        Signal(
            agent_id="macro",
            ticker="IONQ",
            direction=Direction.LONG,
            confidence=70.0,
            reasoning="zone test macro",
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
    assert "zones" in payload
    assert "zones_series" in payload
    assert Path(result["zones_history"]).is_file()
    zones = payload["zones"]
    assert "points" in zones and "as_of" in zones and "day_local" in zones
    tickers = {p["ticker"] for p in zones["points"]}
    assert "IONQ" in tickers
    ionq = next(p for p in zones["points"] if p["ticker"] == "IONQ")
    assert ionq["buy"] > 0
    assert "agents" in ionq
    blob = json.dumps(payload)
    assert "ALPACA_SECRET" not in blob
    assert "secret_key" not in blob
    assert "api_key" not in blob.lower() or "api_key" not in json.dumps(zones).lower()
