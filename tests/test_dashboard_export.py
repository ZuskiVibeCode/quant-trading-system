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


def test_local_day_bounds_evening_et():
    """Evening ET (next calendar day UTC) still belongs to the ET local day."""
    from datetime import datetime, timezone

    from quant_system.dashboard_export import _local_day_start_utc_iso, local_day_bounds

    # 2026-09-21 20:40 ET == 2026-09-22 00:40 UTC (EDT, UTC-4)
    evening_utc = datetime(2026, 9, 22, 0, 40, 31, tzinfo=timezone.utc)
    start_utc, end_utc, local_day = local_day_bounds(evening_utc)
    assert local_day == "2026-09-21"
    assert start_utc.year == 2026 and start_utc.month == 9 and start_utc.day == 21
    assert start_utc.hour == 4  # EDT midnight = 04:00 UTC
    assert end_utc.day == 22 and end_utc.hour == 4
    iso = _local_day_start_utc_iso(evening_utc)
    assert iso.startswith("2026-09-21T04:00:00")
    assert iso.endswith("Z")


def test_build_payload_uses_et_day_for_evening_signals(tmp_path, monkeypatch):
    """Signals stamped after UTC midnight but still on ET day appear in agents_today."""
    from datetime import datetime, timezone

    from quant_system import dashboard_export as de
    from quant_system.models.signal import Direction, Signal
    from quant_system.tracker.api import Tracker

    # Freeze "now" at 2026-09-22 00:40 UTC == Sep 21 20:40 ET
    frozen = datetime(2026, 9, 22, 0, 40, 0, tzinfo=timezone.utc)

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen.replace(tzinfo=None)
            return frozen.astimezone(tz)

    monkeypatch.setattr(de, "datetime", _FrozenDateTime)

    db = str(tmp_path / "etday.db")
    tracker = Tracker(db)
    sig = Signal(
        agent_id="momentum",
        ticker="NVDA",
        direction=Direction.LONG,
        confidence=70.0,
        reasoning="et-day evening signal",
        supporting_data={"rsi": 58.0},
    )
    sid = tracker.insert_signal(sig)
    # Backdate created_at into Sep 21 ET afternoon (still before frozen now, after ET midnight)
    tracker.conn.execute(
        "UPDATE signals SET created_at = ? WHERE id = ?",
        ("2026-09-21T19:48:00.000000Z", sid),
    )
    tracker.conn.commit()

    payload = de.build_dashboard_payload(tracker, include_alpaca=False)
    tracker.close()

    assert payload["meta"]["day_local"] == "2026-09-21"
    assert payload["meta"]["day_timezone"] == "America/New_York"
    assert payload["meta"]["day_label"] == "ET day"
    assert payload["meta"]["day_start_utc"].startswith("2026-09-21T04:00:00")
    assert payload["agents_today"]["momentum"]["count"] >= 1
    assert payload["agents_today"]["momentum"]["signals"][0]["ticker"] == "NVDA"
