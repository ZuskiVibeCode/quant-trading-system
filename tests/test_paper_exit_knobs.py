"""Paper exit knobs v1: asymmetric sell confidence + cash buffer trim."""

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
    db = tmp_path / "exit_knobs.db"
    tracker = Tracker(str(db))
    tracker.set_state("cash", "100.0")
    tracker.set_state("equity", "100.0")
    tracker.set_state("peak_equity", "100.0")
    tracker.set_state("day_start_equity", "100.0")
    md = MockMarketData(
        prices={
            "IONQ": 10.0,
            "NVDA": 20.0,
            "RKLB": 8.0,
            "SPY": 100.0,
        }
    )
    cfg = load_config()
    assert cfg.risk.min_confidence_to_trade == 55.0
    assert cfg.risk.min_confidence_to_sell == 40.0
    assert cfg.risk.cash_buffer_min_pct == 0.03
    assert cfg.risk.cash_buffer_target_pct == 0.05
    assert cfg.risk.buffer_trim_frac == 0.20
    assert cfg.risk.max_buffer_trims_per_cycle == 1
    yield tracker, md, cfg
    tracker.close()


def _sig(ticker: str, direction: Direction, confidence: float, agent_id: str = "momentum") -> Signal:
    return Signal(
        agent_id=agent_id,
        ticker=ticker,
        direction=direction,
        confidence=confidence,
        reasoning="test",
        supporting_data={"test": True},
    )


def _seed_position(tracker: Tracker, ticker: str, qty: float, avg_cost: float) -> None:
    tracker.reconcile_portfolio(
        cash=float(tracker.get_state("cash", "0")),
        equity=float(tracker.get_state("equity", "0")),
        positions=[{"ticker": ticker, "qty": qty, "avg_cost": avg_cost}],
    )


def test_config_exit_knob_defaults(env):
    _, _, cfg = env
    assert cfg.risk.min_confidence_to_sell == 40.0
    assert cfg.risk.min_confidence_to_trade == 55.0


def test_bearish_conf_45_sells(env):
    """Bearish hold with conf 45 → SELL (sell floor 40)."""
    tracker, md, cfg = env
    tracker.reconcile_portfolio(
        cash=50.0,
        equity=100.0,
        positions=[{"ticker": "IONQ", "qty": 5.0, "avg_cost": 10.0}],
    )
    sig = _sig("IONQ", Direction.SHORT, 45.0)
    sid = tracker.insert_signal(sig)
    pm = PortfolioManager(tracker, md, cfg)
    decisions = pm.weigh_and_decide([(sid, sig)])
    sells = [
        d
        for d in decisions
        if isinstance(d, TradeTicket) and d.side == TicketSide.SELL and "cash buffer trim" not in d.rationale
    ]
    assert sells, f"expected SELL, got {decisions}"
    assert sells[0].ticker == "IONQ"


def test_bearish_conf_35_holds(env):
    """Same bearish with conf 35 → HOLD (below sell floor 40)."""
    tracker, md, cfg = env
    # Keep cash high so buffer trim does not fire
    tracker.reconcile_portfolio(
        cash=50.0,
        equity=100.0,
        positions=[{"ticker": "IONQ", "qty": 5.0, "avg_cost": 10.0}],
    )
    sig = _sig("IONQ", Direction.SHORT, 35.0)
    sid = tracker.insert_signal(sig)
    pm = PortfolioManager(tracker, md, cfg)
    decisions = pm.weigh_and_decide([(sid, sig)])
    assert all(not isinstance(d, TradeTicket) for d in decisions), decisions
    holds = [d for d in decisions if isinstance(d, HoldDecision)]
    assert holds
    assert "sell min=40" in holds[0].rationale or "sell min=40.0" in holds[0].rationale


def test_bullish_conf_50_holds_buy_floor(env):
    """Bullish with conf 50 → HOLD (buy floor 55)."""
    tracker, md, cfg = env
    sig = _sig("IONQ", Direction.LONG, 50.0)
    sid = tracker.insert_signal(sig)
    pm = PortfolioManager(tracker, md, cfg)
    decisions = pm.weigh_and_decide([(sid, sig)])
    assert all(not isinstance(d, TradeTicket) for d in decisions), decisions
    holds = [d for d in decisions if isinstance(d, HoldDecision)]
    assert holds
    assert "buy min=55" in holds[0].rationale or "buy min=55.0" in holds[0].rationale


def test_cash_buffer_trim_weakest_score_le_zero(env):
    """Cash < 3%, no buys, weakest score<=0 → one buffer SELL."""
    tracker, md, cfg = env
    # cash 2, IONQ 5*10=50, NVDA 2.25*20=45 → equity 100; cash/eq=2%
    tracker.reconcile_portfolio(
        cash=2.0,
        equity=100.0,
        positions=[
            {"ticker": "IONQ", "qty": 5.0, "avg_cost": 10.0},
            {"ticker": "NVDA", "qty": 2.25, "avg_cost": 20.0},
        ],
    )
    # conf 35 → HOLD on sell floor, but score < 0 retained for trim
    # IONQ more bearish (lower score) than NVDA if both short; use one signal each
    # Single-agent weight 0.2: score = -conf/100 * 0.2
    # IONQ conf 35 → score -0.07; NVDA conf 30 → score -0.06 → IONQ weaker
    s_ionq = _sig("IONQ", Direction.SHORT, 35.0)
    s_nvda = _sig("NVDA", Direction.SHORT, 30.0)
    sid_i = tracker.insert_signal(s_ionq)
    sid_n = tracker.insert_signal(s_nvda)
    pm = PortfolioManager(tracker, md, cfg)
    decisions = pm.weigh_and_decide([(sid_i, s_ionq), (sid_n, s_nvda)])
    buffer_sells = [
        d
        for d in decisions
        if isinstance(d, TradeTicket) and "cash buffer trim" in d.rationale
    ]
    assert len(buffer_sells) == 1, decisions
    assert buffer_sells[0].ticker == "IONQ"
    assert buffer_sells[0].side == TicketSide.SELL
    assert buffer_sells[0].qty == pytest.approx(round(5.0 * 0.20, 6))


def test_no_buffer_trim_when_all_scores_positive(env):
    """All holdings score>0 → no buffer trim even if cash low."""
    tracker, md, cfg = env
    tracker.reconcile_portfolio(
        cash=2.0,
        equity=100.0,
        positions=[
            {"ticker": "IONQ", "qty": 5.0, "avg_cost": 10.0},
            {"ticker": "NVDA", "qty": 2.25, "avg_cost": 20.0},
        ],
    )
    # Bullish but below buy floor → HOLD with score > 0; no buys; cash low
    s_ionq = _sig("IONQ", Direction.LONG, 50.0)
    s_nvda = _sig("NVDA", Direction.LONG, 50.0)
    sid_i = tracker.insert_signal(s_ionq)
    sid_n = tracker.insert_signal(s_nvda)
    pm = PortfolioManager(tracker, md, cfg)
    decisions = pm.weigh_and_decide([(sid_i, s_ionq), (sid_n, s_nvda)])
    buffer_sells = [
        d
        for d in decisions
        if isinstance(d, TradeTicket) and "cash buffer trim" in d.rationale
    ]
    assert buffer_sells == [], decisions
    assert all(not isinstance(d, TradeTicket) for d in decisions)


def test_max_one_buffer_trim_per_cycle(env):
    """Even with two score<=0 holdings, only one buffer trim when max=1."""
    tracker, md, cfg = env
    assert cfg.risk.max_buffer_trims_per_cycle == 1
    tracker.reconcile_portfolio(
        cash=2.0,
        equity=100.0,
        positions=[
            {"ticker": "IONQ", "qty": 5.0, "avg_cost": 10.0},
            {"ticker": "NVDA", "qty": 2.25, "avg_cost": 20.0},
        ],
    )
    s_ionq = _sig("IONQ", Direction.SHORT, 35.0)
    s_nvda = _sig("NVDA", Direction.SHORT, 35.0)
    sid_i = tracker.insert_signal(s_ionq)
    sid_n = tracker.insert_signal(s_nvda)
    pm = PortfolioManager(tracker, md, cfg)
    decisions = pm.weigh_and_decide([(sid_i, s_ionq), (sid_n, s_nvda)])
    buffer_sells = [
        d
        for d in decisions
        if isinstance(d, TradeTicket) and "cash buffer trim" in d.rationale
    ]
    assert len(buffer_sells) == 1, decisions
