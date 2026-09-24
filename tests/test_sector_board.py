"""Unit tests for Orbit sector_board builder (pressure / breadth / book / tape)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from quant_system.dashboard_export import (
    build_sector_board,
    compute_theme_tape_returns,
    export_dashboard,
    validate_payload,
)
from quant_system.market_data.interface import MarketData
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


class _FrameMarketData(MarketData):
    """Deterministic Close histories for tape unit tests (no network)."""

    def __init__(self, closes: dict[str, list[float]]):
        self.closes = {k.upper(): list(v) for k, v in closes.items()}
        self.calls: list[str] = []

    def history(self, ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        t = ticker.upper()
        self.calls.append(t)
        if t not in self.closes:
            raise ValueError(f"No history for {t}")
        vals = self.closes[t]
        idx = pd.date_range(end="2026-09-24", periods=len(vals), freq="B")
        return pd.DataFrame(
            {
                "Open": vals,
                "High": [v * 1.01 for v in vals],
                "Low": [v * 0.99 for v in vals],
                "Close": vals,
                "Volume": [1_000_000] * len(vals),
            },
            index=idx,
        )

    def last_price(self, ticker: str) -> float:
        return float(self.history(ticker)["Close"].iloc[-1])

    def snapshot(self, ticker: str) -> dict:
        df = self.history(ticker)
        return {"ticker": ticker.upper(), "last_close": float(df["Close"].iloc[-1]), "bars": len(df)}


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
    # No market_data → null tape
    assert q["returns"] == {"1d": None, "5d": None, "20d": None}
    assert q["returns_n"] == {"1d": 0, "5d": 0, "20d": 0}

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
    assert board[0]["returns"]["1d"] is None


def test_compute_theme_tape_returns_equal_weight():
    # 21 closes so 20d is available: last / first - 1 for 20d
    # IONQ: ends at 110, 1d ago 100 → +10%; 5d ago 100 → +10%; 20d ago 100 → +10%
    # RGTI: ends at 95, 1d ago 100 → -5%; missing deep history for 20d (only 6 bars)
    ionq = [100.0] * 20 + [110.0]
    # Make day -5 and -1 also 100 so 1d/5d both +10%
    ionq[-2] = 100.0
    ionq[-6] = 100.0
    rgti = [100.0, 100.0, 100.0, 100.0, 100.0, 95.0]  # 6 bars → 1d & 5d ok, 20d skip
    md = _FrameMarketData({"IONQ": ionq, "RGTI": rgti})
    rets, ns = compute_theme_tape_returns(["IONQ", "RGTI", "MISSING"], md)
    # 1d: IONQ +0.10, RGTI -0.05 → mean +0.025
    assert abs(rets["1d"] - 0.025) < 1e-9
    assert ns["1d"] == 2
    # 5d: IONQ 110/100-1=0.10, RGTI 95/100-1=-0.05 → 0.025
    assert abs(rets["5d"] - 0.025) < 1e-9
    assert ns["5d"] == 2
    # 20d: only IONQ 110/100-1=0.10
    assert abs(rets["20d"] - 0.10) < 1e-9
    assert ns["20d"] == 1
    # MISSING never fetched successfully; cache avoids thrash — IONQ+RGTI+MISSING = 3 unique calls
    assert set(md.calls) == {"IONQ", "RGTI", "MISSING"}
    # Second pass uses cache (no extra calls)
    before = len(md.calls)
    compute_theme_tape_returns(["IONQ", "RGTI"], md, cache={})
    # fresh cache still hits provider
    assert len(md.calls) > before


def test_compute_theme_tape_returns_offline_null():
    rets, ns = compute_theme_tape_returns(["IONQ"], None)
    assert rets == {"1d": None, "5d": None, "20d": None}
    assert ns == {"1d": 0, "5d": 0, "20d": 0}

    class Boom(MarketData):
        def history(self, ticker, period="1y", interval="1d"):
            raise RuntimeError("network down")

        def last_price(self, ticker):
            raise RuntimeError("network down")

        def snapshot(self, ticker):
            raise RuntimeError("network down")

    rets2, ns2 = compute_theme_tape_returns(["IONQ"], Boom())
    assert rets2["1d"] is None and ns2["1d"] == 0


def test_build_sector_board_includes_tape_from_market_data():
    # Shared closes: enough for all horizons; quantum eq-wt of IONQ(+10% 1d) and RGTI(-5% 1d)
    ionq = [100.0] * 20 + [110.0]
    ionq[-2] = 100.0
    ionq[-6] = 100.0
    rgti = [100.0] * 20 + [95.0]
    rgti[-2] = 100.0
    rgti[-6] = 100.0
    rklb = [50.0] * 20 + [55.0]
    rklb[-2] = 50.0
    path = [20.0] * 21
    nvda = [100.0] * 21
    md = _FrameMarketData(
        {"IONQ": ionq, "RGTI": rgti, "RKLB": rklb, "PATH": path, "NVDA": nvda}
    )
    board = build_sector_board(
        _agents_fixture(), SECTORS, HOLDINGS, equity=100.0, market_data=md
    )
    by = {r["theme"]: r for r in board}
    q = by["quantum"]
    assert abs(q["returns"]["1d"] - 0.025) < 1e-9
    assert q["returns_n"]["1d"] == 2
    # Pressure still present alongside tape
    assert abs(q["buy"] - 0.4) < 1e-9
    s = by["space"]
    assert abs(s["returns"]["1d"] - 0.10) < 1e-9
    assert s["returns_n"]["1d"] == 1
    # Cache: each ticker fetched once across themes
    assert md.calls.count("IONQ") == 1
    assert md.calls.count("RKLB") == 1


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
    assert "returns" in ionq_theme
    assert set(ionq_theme["returns"]) == {"1d", "5d", "20d"}
    assert "returns_n" in ionq_theme
    blob = json.dumps(payload)
    assert "ALPACA_SECRET" not in blob
    assert "secret_key" not in blob


def test_slim_latest_preserves_returns_on_sector_board():
    from scripts.build_orbit_slim_deploy import slim_latest

    board = [
        {
            "theme": "quantum",
            "buy": 0.1,
            "sell": 0.0,
            "net": 0.1,
            "returns": {"1d": 0.01, "5d": -0.02, "20d": 0.05},
            "returns_n": {"1d": 8, "5d": 8, "20d": 7},
        }
    ]
    out = slim_latest({"sectors": {"quantum": ["IONQ"]}, "sector_board": board})
    assert out["sector_board"] == board
    assert out["sector_board"][0]["returns"]["1d"] == 0.01
