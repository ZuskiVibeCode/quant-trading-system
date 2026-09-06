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
from quant_system.research.fundamental import FundamentalAgent
from quant_system.research.macro import MacroAgent
from quant_system.research.momentum import MomentumAgent
from quant_system.research.sentiment import SentimentAgent
from quant_system.research.statistical_ml import StatisticalMLAgent
from quant_system.tracker.api import Tracker


def build_market_data(*, mock: bool = False, mock_prices: dict[str, float] | None = None) -> MarketData:
    if mock:
        return MockMarketData(prices=mock_prices)
    return YFinanceMarketData()


def build_agents(market_data: MarketData) -> list:
    return [
        MomentumAgent(market_data),
        FundamentalAgent(market_data),
        StatisticalMLAgent(market_data),
        SentimentAgent(market_data),
        MacroAgent(market_data),
    ]


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
    agents = build_agents(md)
    pm = PortfolioManager(tr, md, cfg)
    ex = ExecutionAgent(tr, md)
    auditor = Auditor(tr, cfg)

    universe = tickers or cfg.universe.tickers
    benchmark = cfg.universe.benchmark
    errors: list[str] = []
    stored: list[tuple[int, Any]] = []

    tr.log_decision(
        decision_type="run_start",
        actor="pipeline",
        reasoning=f"run-once start; mode={ex.mode}; tickers={len(universe)}; halted={tr.is_halted()}",
        payload={"tickers": universe, "mode": ex.mode, "paper": cfg.risk.paper_mode},
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
            result = ex.execute(d)
            if result:
                fills.append(result)

    summary = {
        "signals": len(stored),
        "decisions": len(decisions),
        "fills": fills,
        "errors": errors,
        "mode": ex.mode,
        "halted": tr.is_halted(),
        "equity": float(tr.get_state("equity", "100")),
        "cash": float(tr.get_state("cash", "100")),
        "positions": tr.list_positions(),
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
        },
        data_snapshot={
            "equity": summary["equity"],
            "cash": summary["cash"],
            "positions": summary["positions"],
        },
    )
    return summary
