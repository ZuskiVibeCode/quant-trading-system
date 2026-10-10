"""Single-cycle orchestration: research -> tracker -> PM -> execution -> auditor."""

from __future__ import annotations

from typing import Any

from quant_system.config.loader import AppConfig, load_config
from quant_system.execution.execution_agent import ExecutionAgent
from quant_system.execution.portfolio_manager import PortfolioManager
from quant_system.market_data.interface import MarketData
from quant_system.market_data.yfinance_provider import MockMarketData, YFinanceMarketData
from quant_system.models.ticket import TradeTicket
from quant_system.oversight.auditor import Auditor
from quant_system.research.fundamental import FundamentalAgent, fetch_fundamentals_with_fallback
from quant_system.research.macro import MacroAgent
from quant_system.research.momentum import MomentumAgent
from quant_system.research.rotation import (
    select_research_universe,
    update_rotation_after_cycle,
)
from quant_system.research.sentiment import SentimentAgent
from quant_system.research.statistical_ml import StatisticalMLAgent
from quant_system.tracker.api import Tracker


def build_market_data(*, mock: bool = False, mock_prices: dict[str, float] | None = None) -> MarketData:
    if mock:
        return MockMarketData(prices=mock_prices)
    return YFinanceMarketData()


def build_agents(
    market_data: MarketData,
    universe: list[str] | None = None,
    *,
    flow_overlays: bool | None = None,
) -> list:
    return [
        MomentumAgent(market_data, universe=universe, flow_overlays=flow_overlays),
        FundamentalAgent(market_data, fundamentals_fetcher=fetch_fundamentals_with_fallback),
        StatisticalMLAgent(market_data, universe=universe),
        SentimentAgent(market_data),
        MacroAgent(market_data),
    ]


def _log_rotation_events(tracker: Tracker, events: dict[str, Any]) -> None:
    for etype, items in (events or {}).items():
        for item in items or []:
            ticker = (item or {}).get("ticker")
            tracker.log_decision(
                decision_type=f"rotation_{etype}",
                actor="research_rotation",
                ticker=ticker,
                reasoning=f"rotation {etype}: {item}",
                payload=item if isinstance(item, dict) else {"event": item},
            )


def run_once(
    *,
    tracker: Tracker | None = None,
    config: AppConfig | None = None,
    market_data: MarketData | None = None,
    tickers: list[str] | None = None,
    mock: bool = False,
) -> dict[str, Any]:
    """Execute one research+decision+execution cycle. Logs everything to Tracker."""
    cfg = config or load_config()
    md = market_data or build_market_data(mock=mock)
    tr = tracker or Tracker()
    # First cycle after ET midnight: daily P&L restarts from the prior close, day halt lifts.
    tr.roll_trading_day()

    # Explicit tickers override rotation; otherwise research active (+ checkup-due cold).
    if tickers is not None:
        universe = list(tickers)
        rotation_enabled = False
    else:
        universe = select_research_universe(tr, cfg)
        rotation_enabled = True

    agents = build_agents(md, universe=universe, flow_overlays=cfg.risk.momentum_flow_overlays)
    pm = PortfolioManager(tr, md, cfg)
    ex = ExecutionAgent(tr, md)
    auditor = Auditor(tr, cfg)

    benchmark = cfg.universe.benchmark
    errors: list[str] = []
    stored: list[tuple[int, Any]] = []

    tr.log_decision(
        decision_type="run_start",
        actor="pipeline",
        reasoning=(
            f"run-once start; mode={ex.mode}; tickers={len(universe)}; "
            f"halted={tr.is_halted()}; rotation={rotation_enabled}"
        ),
        payload={
            "tickers": universe,
            "mode": ex.mode,
            "paper": cfg.risk.paper_mode,
            "rotation": rotation_enabled,
        },
        data_snapshot={"equity": tr.get_state("equity"), "cash": tr.get_state("cash")},
    )

    for ticker in universe:
        for agent in agents:
            try:
                sig = agent.analyze(ticker, benchmark=benchmark)
                sid = tr.insert_signal(sig)
                stored.append((sid, sig))
            except Exception as exc:  # noqa: BLE001
                msg = f"{agent.agent_id}/{ticker}: {exc}"
                errors.append(msg)
                tr.log_decision(
                    decision_type="research_error",
                    actor=agent.agent_id,
                    ticker=ticker,
                    reasoning=msg,
                    payload={"error": str(exc)},
                )

    auditor.review_signals(stored)
    decisions = pm.weigh_and_decide(stored)

    fills: list[dict[str, Any]] = []
    for d in decisions:
        if isinstance(d, TradeTicket):
            auditor.review_ticket(d, equity=float(tr.get_state("equity", "100")))
            try:
                result = ex.execute(d)
            except Exception as exc:  # noqa: BLE001 — never crash run-once on broker errors
                msg = f"execution/{d.ticker}: {exc}"
                errors.append(msg)
                tr.log_decision(
                    decision_type="order_failed",
                    actor="execution_agent",
                    ticker=d.ticker,
                    reasoning=msg,
                    payload={"error": str(exc), "ticket_id": d.id},
                )
                continue
            if result:
                fills.append(result)

    positions = tr.list_positions()
    rotation_events: dict[str, Any] | None = None
    if rotation_enabled:
        signals_summary = [
            {
                "ticker": getattr(sig, "ticker", None),
                "direction": getattr(getattr(sig, "direction", None), "value", getattr(sig, "direction", None)),
                "confidence": getattr(sig, "confidence", None),
                "agent_id": getattr(sig, "agent_id", None),
            }
            for _sid, sig in stored
        ]
        rotation_events = update_rotation_after_cycle(
            tr, cfg, signals_summary=signals_summary, holdings=positions
        )
        _log_rotation_events(tr, rotation_events)

    summary = {
        "signals": len(stored),
        "decisions": len(decisions),
        "fills": fills,
        "errors": errors,
        "mode": ex.mode,
        "halted": tr.is_halted(),
        "equity": float(tr.get_state("equity", "100")),
        "cash": float(tr.get_state("cash", "100")),
        "positions": positions,
        "research_tickers": universe,
        "rotation": rotation_events,
    }
    auditor.review_run(summary)
    tr.log_decision(
        decision_type="run_end",
        actor="pipeline",
        reasoning=(
            f"run-once complete: {summary['signals']} signals, "
            f"{len(fills)} fills, {len(errors)} errors"
        ),
        payload={
            "signals": summary["signals"],
            "fills": len(fills),
            "errors": errors,
            "mode": ex.mode,
            "research_tickers": universe,
            "rotation": rotation_events,
        },
        data_snapshot={
            "equity": summary["equity"],
            "cash": summary["cash"],
            "positions": summary["positions"],
        },
    )
    return summary
