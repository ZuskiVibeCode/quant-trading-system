"""Next-open fill queue, buy-and-hold benchmarks, and Sharpe/CAGR metrics."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from quant_system.backtest.asof_market_data import AsOfMarketData
from quant_system.backtest.benchmarks import buy_and_hold_curve
from quant_system.backtest.harness import SimExecutionAgent
from quant_system.backtest.metrics import (
    benchmark_comparison,
    cagr,
    curve_stats,
    sharpe_ratio,
)
from quant_system.models.ticket import TicketSide, TicketStatus, TradeTicket
from quant_system.tracker.api import Tracker

DAYS = pd.bdate_range("2024-03-04", periods=5)


def _bars(opens: list[float], closes: list[float], index=DAYS) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": opens,
            "High": [max(o, c) + 1 for o, c in zip(opens, closes)],
            "Low": [min(o, c) - 1 for o, c in zip(opens, closes)],
            "Close": closes,
            "Volume": 1_000_000,
        },
        index=index,
    )


def _tracker(tmp_path, cash: float = 100.0) -> Tracker:
    tr = Tracker(str(tmp_path / "sleeve.db"))
    for key in ("cash", "equity", "peak_equity", "day_start_equity"):
        tr.set_state(key, f"{cash:.6f}")
    return tr


def _ticket(tr: Tracker, ticker: str, side: TicketSide, qty: float) -> TradeTicket:
    t = TradeTicket(ticker=ticker, side=side, qty=qty, rationale="test", signal_ids=[])
    t.id = tr.insert_ticket(t)
    return t


def test_next_open_fills_at_following_session_open_not_signal_bar(tmp_path):
    md = AsOfMarketData({"AAA": _bars([10, 11, 12, 13, 14], [10.5, 11.5, 12.5, 13.5, 14.5])})
    tr = _tracker(tmp_path)
    ex = SimExecutionAgent(tr, md, slippage_bps=10.0)

    md.set_as_of(DAYS[1])  # signal at day-1 close (11.5)
    t = _ticket(tr, "AAA", TicketSide.BUY, 2.0)
    assert ex.execute(t) is None
    assert tr.list_positions() == []  # nothing fills on the signal bar

    md.set_as_of(DAYS[2])
    fills, cancelled = ex.fill_pending()
    assert cancelled == []
    assert fills[0]["price"] == pytest.approx(12.0 * 1.001)  # day-2 open + slippage
    assert float(tr.get_state("cash")) == pytest.approx(100.0 - 2 * 12.0 * 1.001)
    assert ex.pending == []
    tr.close()


def test_next_open_cancels_when_no_bar_instead_of_stale_price(tmp_path):
    # BBB has no bar on DAYS[2] (e.g. halted) — must not fill at DAYS[1] prices.
    gappy = _bars([20, 21, 23, 24], [20, 21, 23, 24], index=DAYS.delete(2))
    md = AsOfMarketData({"BBB": gappy})
    tr = _tracker(tmp_path)
    ex = SimExecutionAgent(tr, md, slippage_bps=0.0)

    md.set_as_of(DAYS[1])
    t = _ticket(tr, "BBB", TicketSide.BUY, 1.0)
    ex.execute(t)
    md.set_as_of(DAYS[2])
    fills, cancelled = ex.fill_pending()
    assert fills == []
    assert cancelled[0]["reason"] == "no bar at next session"
    assert tr.list_positions() == []
    tr.close()


def test_next_open_sells_first_and_trims_buys_to_cash(tmp_path):
    md = AsOfMarketData(
        {
            "AAA": _bars([10] * 5, [10] * 5),
            "CCC": _bars([50] * 5, [50] * 5),
        }
    )
    tr = _tracker(tmp_path, cash=10.0)
    md.set_as_of(DAYS[0])
    seed = _ticket(tr, "AAA", TicketSide.BUY, 1.0)
    tr.insert_fill(ticket_id=seed.id, ticker="AAA", side="buy", qty=1.0, price=10.0, mode="dry_run")
    assert float(tr.get_state("cash")) == pytest.approx(0.0)

    ex = SimExecutionAgent(tr, md, slippage_bps=0.0)
    md.set_as_of(DAYS[1])
    buy = _ticket(tr, "CCC", TicketSide.BUY, 1.0)  # costs 50, only 10 after the sell
    sell = _ticket(tr, "AAA", TicketSide.SELL, 1.0)
    ex.execute(buy)
    ex.execute(sell)
    md.set_as_of(DAYS[2])
    fills, cancelled = ex.fill_pending()

    assert [f["side"] for f in fills] == ["sell", "buy"]
    assert fills[1]["qty"] == pytest.approx(10.0 / 50.0)
    assert fills[1]["trimmed"] is True
    assert cancelled == []
    assert float(tr.get_state("cash")) == pytest.approx(0.0)
    tr.close()


def test_cancel_pending_after_last_day(tmp_path):
    md = AsOfMarketData({"AAA": _bars([10] * 5, [10] * 5)})
    tr = _tracker(tmp_path)
    ex = SimExecutionAgent(tr, md)
    md.set_as_of(DAYS[-1])
    t = _ticket(tr, "AAA", TicketSide.BUY, 1.0)
    ex.execute(t)
    cancelled = ex.cancel_pending("still queued after last day")
    assert len(cancelled) == 1 and ex.pending == []
    assert tr.get_ticket(t.id)["status"] == TicketStatus.CANCELLED.value
    tr.close()


def test_close_fill_mode_still_fills_immediately(tmp_path):
    md = AsOfMarketData({"AAA": _bars([10, 11, 12, 13, 14], [10.5, 11.5, 12.5, 13.5, 14.5])})
    tr = _tracker(tmp_path)
    ex = SimExecutionAgent(tr, md, slippage_bps=0.0, fill_on="close")
    md.set_as_of(DAYS[1])
    fill = ex.execute(_ticket(tr, "AAA", TicketSide.BUY, 1.0))
    assert fill is not None and fill["price"] == pytest.approx(11.5)


def test_unknown_fill_mode_rejected(tmp_path):
    with pytest.raises(ValueError):
        SimExecutionAgent(_tracker(tmp_path), AsOfMarketData({}), fill_on="open")


def test_session_open_never_peeks_or_goes_stale():
    md = AsOfMarketData({"AAA": _bars([10, 11, 12, 13, 14], [10] * 5, index=DAYS)})
    md.set_as_of(DAYS[2])
    assert md.session_open("AAA") == 12.0
    md.set_as_of(DAYS[-1] + pd.Timedelta(days=3))  # after the last bar
    assert md.session_open("AAA") is None


def test_buy_and_hold_next_open_enters_on_second_day():
    bars = {
        "AAA": _bars([10, 20, 20, 20, 20], [10, 20, 22, 24, 30]),
        "BBB": _bars([5, 10, 10, 10, 10], [5, 10, 10, 10, 5]),
    }
    out = buy_and_hold_curve(
        bars, ["AAA", "BBB"], list(DAYS), cash=100.0, slippage_bps=0.0, fill_on="next_open"
    )
    curve = out["equity_curve"]
    assert len(curve) == len(DAYS) + 1
    assert curve[:2] == [100.0, 100.0]  # cash until the day-2 open
    assert out["entry_date"] == str(DAYS[1].date())
    # $50 each at opens 20 and 10 → 2.5 AAA + 5 BBB, marked at closes
    assert curve[2] == pytest.approx(2.5 * 20 + 5 * 10)
    assert curve[-1] == pytest.approx(2.5 * 30 + 5 * 5)


def test_buy_and_hold_excludes_names_without_entry_bar_and_charges_slippage():
    late = _bars([1, 1], [1, 1], index=DAYS[3:])  # lists after entry
    bars = {"AAA": _bars([10] * 5, [10] * 5), "NEW": late}
    out = buy_and_hold_curve(
        bars, ["AAA", "NEW"], list(DAYS), cash=100.0, slippage_bps=100.0, fill_on="close"
    )
    assert out["tickers"] == ["AAA"]
    assert out["excluded"] == ["NEW"]
    assert out["equity_curve"][1] == pytest.approx(100.0 / 1.01)


def test_sharpe_cagr_and_curve_stats():
    assert sharpe_ratio([100.0, 100.0, 100.0]) == 0.0
    assert sharpe_ratio([100.0]) == 0.0
    rising = [100.0 * (1.001**i) * (1 + (0.002 if i % 2 else -0.002)) for i in range(50)]
    assert sharpe_ratio(rising) > 0
    # One year of steps that doubles → 100% CAGR
    one_year = [100.0] + [100.0] * 251 + [200.0]
    assert cagr(one_year) == pytest.approx(1.0)
    stats = curve_stats([100.0, 90.0, 110.0])
    assert stats["return_pct"] == pytest.approx(0.10)
    assert stats["max_dd"] == pytest.approx(0.10)
    assert not math.isnan(stats["sharpe"])


def test_benchmark_comparison_requires_beating_every_benchmark():
    strat = {"return_pct": 0.10, "cagr": 0.10, "sharpe": 1.2, "max_dd": 0.1}
    spy = {"return_pct": 0.05, "cagr": 0.05, "sharpe": 0.8, "max_dd": 0.1}
    ew_better_sharpe = {"return_pct": 0.04, "cagr": 0.04, "sharpe": 1.5, "max_dd": 0.1}
    assert benchmark_comparison(strat, {"spy": spy})["beats_benchmarks"] is True
    out = benchmark_comparison(strat, {"spy": spy, "equal_weight": ew_better_sharpe})
    assert out["beats_benchmarks"] is False
    assert out["vs_benchmarks"]["spy"]["excess_return"] == pytest.approx(0.05)
    assert benchmark_comparison(strat, {})["beats_benchmarks"] is False
