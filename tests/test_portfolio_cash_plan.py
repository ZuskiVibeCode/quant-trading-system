"""PortfolioManager plans buys against cumulative remaining cash (no BP over-commit)."""

from __future__ import annotations

import pytest

from quant_system.config.loader import load_config
from quant_system.execution.portfolio_manager import PortfolioManager
from quant_system.market_data.yfinance_provider import MockMarketData
from quant_system.models.signal import Direction, Signal
from quant_system.models.ticket import HoldDecision, TicketSide, TradeTicket
from quant_system.tracker.api import Tracker


@pytest.fixture()
def env(tmp_path):
    db = tmp_path / "cash_plan.db"
    tracker = Tracker(str(db))
    md = MockMarketData(
        prices={
            "AAA": 5.0,
            "BBB": 4.0,
            "CCC": 3.0,
            "DDD": 2.5,
            "EEE": 2.0,
            "FFF": 1.5,
            "HELD": 10.0,
            "SPY": 100.0,
        }
    )
    cfg = load_config()
    cfg.risk.min_confidence_to_trade = 1.0
    cfg.risk.min_confidence_to_sell = 1.0
    cfg.risk.min_order_notional = 1.0
    cfg.risk.starting_equity = 100.0
    yield tracker, md, cfg
    tracker.close()


def _force_long(ticker: str, confidence: float = 90.0) -> Signal:
    return Signal(
        agent_id="momentum",
        ticker=ticker,
        direction=Direction.LONG,
        confidence=confidence,
        reasoning=f"forced long {ticker}",
        supporting_data={"test": True},
    )


def _force_short(ticker: str, confidence: float = 90.0) -> Signal:
    return Signal(
        agent_id="momentum",
        ticker=ticker,
        direction=Direction.SHORT,
        confidence=confidence,
        reasoning=f"forced short {ticker}",
        supporting_data={"test": True},
    )


def test_buys_do_not_overcommit_cash(env):
    """With $10 cash, total BUY notional must stay within cash*0.95."""
    tracker, md, cfg = env
    # Equity ~$100 via a held name so max_position_pct room is not the binding cap;
    # cash ($10) is. Before the fix every buy was sized as cash*0.95 independently.
    tracker.reconcile_portfolio(
        cash=10.0,
        equity=100.0,
        positions=[{"ticker": "HELD", "qty": 9.0, "avg_cost": 10.0}],
        source="test",
    )
    tracker.set_state("peak_equity", "100.0")
    tracker.set_state("day_start_equity", "100.0")

    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    # Distinct confidences so weighted scores differ (rank order deterministic)
    signals: list[tuple[int, Signal]] = []
    for i, t in enumerate(tickers):
        sig = _force_long(t, confidence=95.0 - i)
        sid = tracker.insert_signal(sig)
        signals.append((sid, sig))

    pm = PortfolioManager(tracker, md, cfg)
    decisions = pm.weigh_and_decide(signals)

    buys = [d for d in decisions if isinstance(d, TradeTicket) and d.side == TicketSide.BUY]
    holds = [d for d in decisions if isinstance(d, HoldDecision)]

    cash = 10.0
    total_notional = 0.0
    for b in buys:
        px = md.last_price(b.ticker)
        total_notional += float(b.qty) * px

    assert total_notional <= cash * 0.95 + 1e-6, (
        f"over-committed: buys={[(b.ticker, b.qty) for b in buys]} "
        f"notional={total_notional:.4f} > {cash * 0.95:.4f}"
    )
    assert buys, "expected at least one BUY from $10 cash"
    # Extra names that could not be funded become HOLD with remaining-cash rationale
    assert len(buys) < len(tickers), "expected some names to be held for cash"
    cash_holds = [
        h
        for h in holds
        if h.ticker in tickers and "insufficient remaining cash" in h.rationale
    ]
    assert cash_holds, f"expected remaining-cash HOLDs, got {[h.rationale for h in holds]}"


def test_sell_proceeds_credit_subsequent_buys(env):
    """SELL frees cash in the plan; buys may use proceeds; sells appear before buys."""
    tracker, md, cfg = env
    # $2 cash alone cannot fund a meaningful buy under room, but selling HELD frees ~$50.
    tracker.reconcile_portfolio(
        cash=2.0,
        equity=52.0,
        positions=[{"ticker": "HELD", "qty": 5.0, "avg_cost": 10.0}],
        source="test",
    )
    tracker.set_state("peak_equity", "52.0")
    tracker.set_state("day_start_equity", "52.0")

    sell_sig = _force_short("HELD", confidence=90.0)
    buy_sig = _force_long("AAA", confidence=95.0)
    sell_sid = tracker.insert_signal(sell_sig)
    buy_sid = tracker.insert_signal(buy_sig)

    pm = PortfolioManager(tracker, md, cfg)
    decisions = pm.weigh_and_decide([(sell_sid, sell_sig), (buy_sid, buy_sig)])

    tickets = [d for d in decisions if isinstance(d, TradeTicket)]
    sells = [t for t in tickets if t.side == TicketSide.SELL]
    buys = [t for t in tickets if t.side == TicketSide.BUY]

    assert sells, "expected SELL of HELD"
    assert any(t.ticker == "HELD" for t in sells)
    assert buys, "expected BUY funded by sell proceeds"
    assert any(t.ticker == "AAA" for t in buys)

    # Return order: first sell index before first buy index
    first_sell = next(i for i, d in enumerate(decisions) if isinstance(d, TradeTicket) and d.side == TicketSide.SELL)
    first_buy = next(i for i, d in enumerate(decisions) if isinstance(d, TradeTicket) and d.side == TicketSide.BUY)
    assert first_sell < first_buy, "sells must precede buys for execution cash frees"

    # Buy notional should exceed starting cash (proves sell credit was used)
    buy = next(t for t in buys if t.ticker == "AAA")
    buy_notional = float(buy.qty) * md.last_price("AAA")
    assert buy_notional > 2.0, f"buy should use sell proceeds, notional={buy_notional}"
