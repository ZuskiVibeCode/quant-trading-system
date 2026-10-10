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


def test_slim_latest_preserves_trade_reviews_and_closed_review():
    from scripts.build_orbit_slim_deploy import slim_latest

    review = {
        "ticker": "IONQ",
        "qty": 1.0,
        "entry": 40.0,
        "exit": 38.0,
        "pnl": -2.0,
        "verdict": "Lost money: Realized PnL=-2.0000.",
        "verdict_tags": ["loser", "split_book"],
        "completeness": "full",
        "evidence": [
            {"kind": "fill", "id": 1, "label": "entry fill", "detail": "buy qty=1 @ 40"},
            {"kind": "signal", "id": 99, "label": "momentum @ entry", "detail": "long @ conf=72.0"},
        ],
        "exit_reason": "signal_sell",
        "mix": {"long": ["momentum"], "short": ["fundamental"], "neutral": [], "disagreement": True, "lead_agent": "momentum"},
        "agents_at_entry": [
            {"agent_id": "momentum", "direction": "long", "confidence": 72, "signal_id": 99, "reasoning_trunc": "x" * 300}
        ],
        "path": {"mfe_pct": None, "mae_pct": None, "ret_pct": -5.0},
    }
    payload = {
        "sectors": {},
        "ticker_pnl": [{"ticker": "IONQ", "review_count": 1, "last_verdict": review["verdict"]}],
        "trade_reviews": [review],
        "closed": [{"ticker": "IONQ", "qty": 1.0, "closed_at": "2026-09-11T10:00:00Z", "pnl": -2.0, "review": review}],
    }
    out = slim_latest(payload)
    assert len(out["trade_reviews"]) == 1
    assert out["trade_reviews"][0]["verdict"].startswith("Lost money")
    assert out["trade_reviews"][0]["evidence"][0]["id"] == 1
    assert out["closed"][0]["review"]["completeness"] == "full"
    assert out["ticker_pnl"][0]["review_count"] == 1
    # reasoning truncated in slim agents
    assert len(out["trade_reviews"][0]["agents_at_entry"][0]["reasoning_trunc"]) <= 141


def test_slim_latest_keeps_why_bought_and_trims_text():
    from scripts.build_orbit_slim_deploy import slim_latest

    fills = [{"fill_id": 1, "at": "2026-10-06T14:01:00Z", "qty": 0.06, "price": 230.0, "mode": "alpaca"}]
    wb = {
        "opened_at": "2026-10-06T14:01:00Z",
        "buy_count": 1,
        "omitted": 0,
        "buys": [
            {
                "ticket_id": 101,
                "created_at": "2026-10-06T14:00:00Z",
                "rationale": "r" * 600,
                "score": {"weighted_score": 0.65},
                "signals": [
                    {"agent_id": "momentum", "direction": "long", "confidence": 78.0,
                     "signal_id": 7, "created_at": "2026-10-06T13:55:00Z", "reasoning_trunc": "x" * 300},
                ],
                "fills": fills,
            }
        ],
    }
    out = slim_latest({"sectors": {}, "holdings": [{"ticker": "NVDA", "qty": 0.06, "why_bought": wb}]})
    slim = out["holdings"][0]["why_bought"]
    assert slim["buy_count"] == 1 and slim["opened_at"] == wb["opened_at"]
    (buy,) = slim["buys"]
    assert buy["ticket_id"] == 101 and buy["fills"] == fills and buy["score"] == {"weighted_score": 0.65}
    assert len(buy["rationale"]) <= 400
    sig = buy["signals"][0]
    assert (sig["agent_id"], sig["direction"], sig["confidence"]) == ("momentum", "long", 78.0)
    assert len(sig["reasoning_trunc"]) <= 140
