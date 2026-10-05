"""Opportunity-cost rotate: trim weakest long to fund a higher-score buy when cash is tight."""

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
    db = tmp_path / "opp_rotate.db"
    tracker = Tracker(str(db))
    # Unit weight so score ≈ conf/100 (clear gaps vs default 0.2 equal weights)
    tracker.set_weight("momentum", 1.0, notes="test")
    for aid in ("fundamental", "statistical_ml", "sentiment", "macro"):
        tracker.set_weight(aid, 0.0, notes="test")
    md = MockMarketData(
        prices={
            "WEAK": 10.0,
            "BEST": 5.0,
            "MID": 8.0,
            "ALT": 4.0,
            "DUST": 10.0,
            "SPY": 100.0,
        }
    )
    cfg = load_config()
    cfg.risk.min_confidence_to_trade = 1.0
    cfg.risk.min_confidence_to_sell = 1.0
    cfg.risk.min_order_notional = 1.0
    cfg.risk.starting_equity = 100.0
    cfg.risk.opportunity_rotate_enabled = True
    cfg.risk.opportunity_rotate_min_score_gap = 0.15
    cfg.risk.opportunity_rotate_trim_frac = 0.25
    cfg.risk.max_opportunity_trims_per_cycle = 2
    cfg.risk.opportunity_rotate_min_buy_score = 0.05
    # Keep cash-buffer trim from interfering
    cfg.risk.cash_buffer_min_pct = 0.0
    yield tracker, md, cfg
    tracker.close()


def _long(ticker: str, confidence: float) -> Signal:
    return Signal(
        agent_id="momentum",
        ticker=ticker,
        direction=Direction.LONG,
        confidence=confidence,
        reasoning=f"forced long {ticker}",
        supporting_data={"test": True},
    )


def test_rotate_weak_long_to_fund_strong_buy(env):
    """Fully invested in weak long → rotate SELL frees cash for strong BUY; sells before buys."""
    tracker, md, cfg = env
    # cash ~$2, WEAK 9.8*$10 ≈ $98 → equity ~$100; room for BEST ≈ $20
    tracker.reconcile_portfolio(
        cash=0.50,
        equity=100.0,
        positions=[{"ticker": "WEAK", "qty": 9.8, "avg_cost": 10.0}],
        source="test",
    )
    tracker.set_state("peak_equity", "100.0")
    tracker.set_state("day_start_equity", "100.0")

    # WEAK still slightly positive (score 0.40); BEST strong (0.95); gap 0.55 >= 0.15
    weak_sig = _long("WEAK", 40.0)
    best_sig = _long("BEST", 95.0)
    signals = [
        (tracker.insert_signal(weak_sig), weak_sig),
        (tracker.insert_signal(best_sig), best_sig),
    ]

    pm = PortfolioManager(tracker, md, cfg)
    decisions = pm.weigh_and_decide(signals)

    tickets = [d for d in decisions if isinstance(d, TradeTicket)]
    rotate_sells = [
        t for t in tickets if t.side == TicketSide.SELL and "opportunity rotate" in t.rationale
    ]
    buys = [t for t in tickets if t.side == TicketSide.BUY]

    assert rotate_sells, f"expected rotate SELL, got {decisions}"
    assert rotate_sells[0].ticker == "WEAK"
    assert any(t.ticker == "BEST" for t in buys), f"expected BEST buy, got {buys}"

    first_sell_i = next(
        i
        for i, d in enumerate(decisions)
        if isinstance(d, TradeTicket) and d.side == TicketSide.SELL
    )
    first_buy_i = next(
        i
        for i, d in enumerate(decisions)
        if isinstance(d, TradeTicket) and d.side == TicketSide.BUY
    )
    assert first_sell_i < first_buy_i

    sell = rotate_sells[0]
    buy = next(t for t in buys if t.ticker == "BEST")
    sell_proceeds = float(sell.qty) * md.last_price("WEAK")
    buy_notional = float(buy.qty) * md.last_price("BEST")
    # Buy funded by sell proceeds (+ tiny starting cash), within float eps
    assert buy_notional <= sell_proceeds + 0.50 * 0.95 + 1e-4
    assert buy_notional > 0.50, "buy should exceed starting cash via rotate proceeds"
    assert "opportunity rotate to fund BEST" in sell.rationale


def test_no_rotate_when_score_gap_too_small(env):
    """Buy score must beat donor by opportunity_rotate_min_score_gap."""
    tracker, md, cfg = env
    cfg.risk.opportunity_rotate_min_score_gap = 0.50  # require large gap

    tracker.reconcile_portfolio(
        cash=0.50,
        equity=100.0,
        positions=[{"ticker": "WEAK", "qty": 9.8, "avg_cost": 10.0}],
        source="test",
    )
    tracker.set_state("peak_equity", "100.0")
    tracker.set_state("day_start_equity", "100.0")

    # scores 0.40 vs 0.70 → gap 0.30 < 0.50 → no rotate
    weak_sig = _long("WEAK", 40.0)
    best_sig = _long("BEST", 70.0)
    signals = [
        (tracker.insert_signal(weak_sig), weak_sig),
        (tracker.insert_signal(best_sig), best_sig),
    ]

    pm = PortfolioManager(tracker, md, cfg)
    decisions = pm.weigh_and_decide(signals)

    rotate_sells = [
        d
        for d in decisions
        if isinstance(d, TradeTicket)
        and d.side == TicketSide.SELL
        and "opportunity rotate" in d.rationale
    ]
    assert rotate_sells == [], decisions
    buys = [d for d in decisions if isinstance(d, TradeTicket) and d.side == TicketSide.BUY]
    assert not any(t.ticker == "BEST" for t in buys)
    holds = [d for d in decisions if isinstance(d, HoldDecision) and d.ticker == "BEST"]
    assert holds and "insufficient remaining cash" in holds[0].rationale


def test_max_opportunity_trims_per_cycle(env):
    """max_opportunity_trims_per_cycle=1 → at most one rotate sell even with multiple unfunded buys."""
    tracker, md, cfg = env
    cfg.risk.max_opportunity_trims_per_cycle = 1

    # Two weak longs; two strong buys; cash near zero
    tracker.reconcile_portfolio(
        cash=1.0,
        equity=100.0,
        positions=[
            {"ticker": "WEAK", "qty": 5.0, "avg_cost": 10.0},
            {"ticker": "MID", "qty": 6.0, "avg_cost": 8.0},
        ],
        source="test",
    )
    tracker.set_state("peak_equity", "100.0")
    tracker.set_state("day_start_equity", "100.0")

    sigs = [
        _long("WEAK", 35.0),  # score 0.35
        _long("MID", 40.0),  # score 0.40
        _long("BEST", 95.0),  # 0.95 — processed first
        _long("ALT", 90.0),  # 0.90 — second unfunded buy
    ]
    signals = [(tracker.insert_signal(s), s) for s in sigs]

    pm = PortfolioManager(tracker, md, cfg)
    decisions = pm.weigh_and_decide(signals)

    rotate_sells = [
        d
        for d in decisions
        if isinstance(d, TradeTicket)
        and d.side == TicketSide.SELL
        and "opportunity rotate" in d.rationale
    ]
    assert len(rotate_sells) == 1, decisions

def test_rotate_skips_dust_donor_tries_next(env):
    """First (weakest) donor under min_order_notional → try next viable donor."""
    tracker, md, cfg = env
    cfg.risk.min_order_notional = 5.0  # DUST entire position is under this

    # DUST ~$0.40 (under min), WEAK ~$98; cash tight so BEST needs rotate
    tracker.reconcile_portfolio(
        cash=0.50,
        equity=100.0,
        positions=[
            {"ticker": "DUST", "qty": 0.04, "avg_cost": 10.0},
            {"ticker": "WEAK", "qty": 9.8, "avg_cost": 10.0},
        ],
        source="test",
    )
    tracker.set_state("peak_equity", "100.0")
    tracker.set_state("day_start_equity", "100.0")

    # DUST weakest (0.20) sorts before WEAK (0.40); BEST 0.95 clears gap vs both
    dust_sig = _long("DUST", 20.0)
    weak_sig = _long("WEAK", 40.0)
    best_sig = _long("BEST", 95.0)
    signals = [
        (tracker.insert_signal(dust_sig), dust_sig),
        (tracker.insert_signal(weak_sig), weak_sig),
        (tracker.insert_signal(best_sig), best_sig),
    ]

    pm = PortfolioManager(tracker, md, cfg)
    decisions = pm.weigh_and_decide(signals)

    rotate_sells = [
        d
        for d in decisions
        if isinstance(d, TradeTicket)
        and d.side == TicketSide.SELL
        and "opportunity rotate" in d.rationale
    ]
    assert rotate_sells, f"expected rotate SELL after skipping dust, got {decisions}"
    assert rotate_sells[0].ticker == "WEAK", (
        f"expected WEAK donor after dust abort, got {rotate_sells[0].ticker}"
    )
    assert all(t.ticker != "DUST" for t in rotate_sells)
    buys = [d for d in decisions if isinstance(d, TradeTicket) and d.side == TicketSide.BUY]
    assert any(t.ticker == "BEST" for t in buys), f"expected BEST buy, got {buys}"
