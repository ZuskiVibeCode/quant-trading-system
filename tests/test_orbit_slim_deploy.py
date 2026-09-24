"""Regression tests for the slim Orbit Vercel deploy payload."""

from scripts.build_orbit_slim_deploy import slim_signal


def test_slim_signal_preserves_supporting_data():
    supporting_data = {"rsi": 42.5, "stub": False}
    slimmed = slim_signal(
        {
            "agent_id": "momentum",
            "ticker": "IONQ",
            "direction": "long",
            "confidence": 70.0,
            "reasoning": "test",
            "supporting_data": supporting_data,
            "created_at": "2026-09-24T14:00:00Z",
        }
    )

    assert slimmed["supporting_data"] == supporting_data


def test_slim_latest_preserves_sector_board():
    from scripts.build_orbit_slim_deploy import slim_latest

    board = [{"theme": "quantum", "buy": 0.1, "sell": 0.0, "net": 0.1}]
    out = slim_latest({"sectors": {"quantum": ["IONQ"]}, "sector_board": board})
    assert out["sector_board"] == board
    assert out["sectors"]["quantum"] == ["IONQ"]


def test_slim_latest_preserves_ticker_pnl():
    from scripts.build_orbit_slim_deploy import slim_latest

    rows = [
        {
            "ticker": "IONQ",
            "sector": "quantum",
            "realized": 1.5,
            "unrealized": None,
            "total": 1.5,
            "qty_open": 0.0,
            "avg_cost_open": None,
            "close_events": 2,
            "buy_events": 2,
            "first_buy_at": "2026-09-01T00:00:00Z",
            "last_activity_at": "2026-09-10T00:00:00Z",
            "series": [
                {"at": "2026-09-05T00:00:00Z", "pnl": 1.0, "cum": 1.0},
                {"at": "2026-09-10T00:00:00Z", "pnl": 0.5, "cum": 1.5},
            ],
        }
    ]
    out = slim_latest({"sectors": {}, "ticker_pnl": rows})
    assert out["ticker_pnl"] == rows
