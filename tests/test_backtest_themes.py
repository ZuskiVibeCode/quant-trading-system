"""Unit tests for theme-competition backtest (as-of, sleeve isolation, e2e leaderboard)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant_system.backtest.asof_market_data import AsOfMarketData
from quant_system.backtest.harness import SimExecutionAgent, run_theme_backtest
from quant_system.backtest.metrics import max_drawdown, rank_leaderboard
from quant_system.config.loader import load_config
from quant_system.models.ticket import TicketSide, TradeTicket
from quant_system.tracker.api import Tracker


def _make_bars(
    ticker: str,
    *,
    start: str = "2024-01-02",
    n: int = 80,
    start_price: float = 50.0,
    drift: float = 0.002,
    seed: int = 0,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed + abs(hash(ticker)) % 10_000)
    idx = pd.bdate_range(start=start, periods=n)
    rets = rng.normal(drift, 0.015, size=n)
    closes = start_price * np.cumprod(1.0 + rets)
    opens = closes * (1.0 + rng.normal(0, 0.002, size=n))
    highs = np.maximum(opens, closes) * 1.01
    lows = np.minimum(opens, closes) * 0.99
    vol = rng.integers(100_000, 900_000, size=n)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vol},
        index=idx,
    )


def _fixture_universe_bars() -> dict[str, pd.DataFrame]:
    """Small deterministic OHLCV set covering three themes + SPY + macro proxies."""
    cfg = load_config()
    bars: dict[str, pd.DataFrame] = {}
    # Divergent drifts so sleeves diverge
    drifts = {"quantum": 0.004, "space": 0.001, "robotics": -0.001}
    for theme, members in cfg.universe.themes.items():
        drift = drifts.get(theme, 0.001)
        for i, t in enumerate(members.active):
            bars[t] = _make_bars(t, n=70, start_price=40 + i, drift=drift, seed=i + 1)
    for t, drift, seed in (
        ("SPY", 0.0005, 99),
        ("^VIX", 0.0, 100),
        ("UUP", 0.0, 101),
        ("TLT", 0.0, 102),
        ("IEF", 0.0, 103),
    ):
        bars[t] = _make_bars(t, n=70, start_price=100.0 if t != "^VIX" else 18.0, drift=drift, seed=seed)
    return bars


def test_asof_truncation_no_lookahead():
    idx = pd.bdate_range("2024-06-03", periods=10)
    closes = np.arange(10, dtype=float) + 100
    df = pd.DataFrame(
        {
            "Open": closes,
            "High": closes + 1,
            "Low": closes - 1,
            "Close": closes,
            "Volume": 1_000_000,
        },
        index=idx,
    )
    md = AsOfMarketData({"AAA": df, "SPY": df})
    asof = idx[4]  # 5th bar
    md.set_as_of(asof)

    hist = md.history("AAA", period="1y")
    assert hist.index.max() == asof
    assert len(hist) == 5
    assert float(hist["Close"].iloc[-1]) == float(closes[4])
    assert md.last_price("AAA") == float(closes[4])

    # Future bars must not leak even if still in cache
    assert (md._bars["AAA"].index > asof).any()
    snap = md.snapshot("AAA")
    assert snap["asof"] == str(asof.date())
    assert snap["bars"] == 5


def test_asof_period_window_respects_asof():
    idx = pd.bdate_range("2023-01-02", periods=300)
    closes = np.linspace(50, 80, 300)
    df = pd.DataFrame(
        {
            "Open": closes,
            "High": closes + 1,
            "Low": closes - 1,
            "Close": closes,
            "Volume": 500_000,
        },
        index=idx,
    )
    md = AsOfMarketData({"BBB": df})
    md.set_as_of(idx[200])
    hist = md.history("BBB", period="5d")
    assert hist.index.max() == idx[200]
    # ~5-10 calendar days of bars
    assert 1 <= len(hist) <= 10


def test_sleeve_isolation_quantum_fill_does_not_touch_space_cash(tmp_path):
    bars = _fixture_universe_bars()
    md = AsOfMarketData(bars)
    day = md.trading_days("2024-01-02", "2024-03-29", reference="SPY")[20]
    md.set_as_of(day)

    q_db = str(tmp_path / "quantum.db")
    s_db = str(tmp_path / "space.db")
    q_tr = Tracker(q_db)
    s_tr = Tracker(s_db)
    for tr in (q_tr, s_tr):
        tr.set_state("cash", "100.000000")
        tr.set_state("equity", "100.000000")
        tr.set_state("peak_equity", "100.000000")
        tr.set_state("day_start_equity", "100.000000")

    space_cash_before = float(s_tr.get_state("cash"))

    # Insert a buy ticket + fill on quantum only
    ticket = TradeTicket(
        ticker="IONQ",
        side=TicketSide.BUY,
        qty=1.0,
        rationale="isolation test",
        signal_ids=[],
    )
    tid = q_tr.insert_ticket(ticket)
    ticket.id = tid
    ex = SimExecutionAgent(q_tr, md, slippage_bps=0.0)
    fill = ex.execute(ticket)
    assert fill is not None
    assert fill["ticker"] == "IONQ"

    assert float(q_tr.get_state("cash")) < 100.0
    assert float(s_tr.get_state("cash")) == pytest.approx(space_cash_before)
    assert s_tr.list_positions() == []
    assert any(p["ticker"] == "IONQ" for p in q_tr.list_positions())

    q_tr.close()
    s_tr.close()


def test_max_drawdown_and_rank():
    assert max_drawdown([100, 110, 90, 95]) == pytest.approx((110 - 90) / 110)
    ranked = rank_leaderboard(
        [
            {"theme": "a", "realized_pnl": 1.0, "ending_equity": 101, "max_dd": 0.1},
            {"theme": "b", "realized_pnl": 5.0, "ending_equity": 105, "max_dd": 0.2},
            {"theme": "c", "realized_pnl": 5.0, "ending_equity": 105, "max_dd": 0.05},
        ]
    )
    assert [r["theme"] for r in ranked] == ["c", "b", "a"]
    assert ranked[0]["rank"] == 1


def test_e2e_fixture_leaderboard(tmp_path):
    bars = _fixture_universe_bars()
    # Use a mid window so agents have lookback history
    idx = bars["SPY"].index
    start = str(idx[25].date())
    end = str(idx[45].date())

    result = run_theme_backtest(
        start=start,
        end=end,
        cash_per_sleeve=100.0,
        bars=bars,
        slippage_bps=5.0,
        output_dir=tmp_path / "out",
        work_dir=tmp_path / "work",
    )

    board = result["leaderboard"]
    assert len(board) == 3
    themes = {r["theme"] for r in board}
    assert themes == {"quantum", "space", "robotics"}
    assert [r["rank"] for r in board] == [1, 2, 3]
    for row in board:
        assert "realized_pnl" in row
        assert "ending_equity" in row
        assert "max_dd" in row
        assert "trade_count" in row
        assert "turnover" in row
        assert row["starting_cash"] == 100.0

    # Agents status documented
    agents = result["meta"]["agents"]
    assert "stubbed" in agents["sentiment"]
    assert "stubbed" in agents["fundamental"]
    assert "live" in agents["momentum"]

    latest = tmp_path / "out" / "theme_leaderboard_latest.json"
    assert latest.is_file()
    loaded = json.loads(latest.read_text())
    assert len(loaded["leaderboard"]) == 3


def test_cli_backtest_themes_help():
    from quant_system.cli import build_parser

    p = build_parser()
    args = p.parse_args(["backtest-themes", "--start", "2024-01-01", "--end", "2024-03-01", "--cash", "100"])
    assert args.command == "backtest-themes"
    assert args.cash == 100.0
