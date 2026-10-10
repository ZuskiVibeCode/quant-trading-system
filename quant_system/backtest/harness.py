"""Theme-competition replay harness — isolated sleeves, as-of bars, sim fills."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from quant_system.backtest.asof_market_data import AsOfMarketData
from quant_system.backtest.benchmarks import BENCHMARK_DOC, buy_and_hold_curve
from quant_system.backtest.cache import MACRO_PROXY_TICKERS, default_cache_dir, ensure_bars
from quant_system.backtest.metrics import (
    benchmark_comparison,
    curve_stats,
    rank_leaderboard,
    sleeve_summary,
)
from quant_system.backtest.stubs import AGENT_STATUS, build_backtest_agents
from quant_system.config.loader import AppConfig, load_config
from quant_system.execution.portfolio_manager import PortfolioManager
from quant_system.models.ticket import TicketStatus, TradeTicket
from quant_system.tracker.api import Tracker

# Fill model: constant bps slippage; buy pays px * (1 + slip), sell receives
# px * (1 - slip). Default next_open = signal at close t, fill at open t+1.
DEFAULT_SLIPPAGE_BPS = 5.0
DEFAULT_CASH_PER_SLEEVE = 100.0
DEFAULT_FILL_ON = "next_open"

FILL_MODEL_DOCS: dict[str, str] = {
    "next_open": (
        "signal at day t close, order queued, filled at day t+1 open + constant "
        "slippage_bps; sells fill before buys and buys are trimmed to available cash; "
        "orders for names with no bar at t+1 are cancelled; orders still queued after "
        "the last day are cancelled"
    ),
    "close": (
        "same-bar close + constant slippage_bps (optimistic: fills on the bar the "
        "signal was computed on); instant full fills"
    ),
}
FILL_MODES = tuple(FILL_MODEL_DOCS)


def _side(ticket: TradeTicket) -> str:
    return ticket.side.value if hasattr(ticket.side, "value") else str(ticket.side)


@dataclass
class SleeveState:
    theme: str
    tickers: list[str]
    tracker: Tracker
    equity_curve: list[float] = field(default_factory=list)
    trade_count: int = 0
    turnover_notional: float = 0.0
    fills: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pending: list[TradeTicket] = field(default_factory=list)
    cancelled: list[dict[str, Any]] = field(default_factory=list)


class SimExecutionAgent:
    """Dry-run fills only (never Alpaca).

    ``fill_on="next_open"``: ``execute`` queues the ticket in ``pending``; call
    ``fill_pending`` after the as-of date moves to the next session.
    ``fill_on="close"``: ``execute`` fills immediately at the as-of close.
    """

    def __init__(
        self,
        tracker: Tracker,
        market_data: AsOfMarketData,
        *,
        slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
        fill_on: str = DEFAULT_FILL_ON,
        pending: list[TradeTicket] | None = None,
    ):
        if fill_on not in FILL_MODES:
            raise ValueError(f"fill_on must be one of {FILL_MODES}, got {fill_on!r}")
        self.tracker = tracker
        self.market_data = market_data
        self.slippage_bps = float(slippage_bps)
        self.fill_on = fill_on
        # Shared with SleeveState so the queue survives across daily agent instances.
        self.pending = pending if pending is not None else []
        self.mode = "backtest_sim"

    def _slipped(self, px: float, side: str) -> float:
        slip = self.slippage_bps / 10_000.0
        if side == "buy":
            return float(px) * (1.0 + slip)
        return float(px) * (1.0 - slip)

    def execute(self, ticket: TradeTicket) -> dict[str, Any] | None:
        """Fill now (close mode) or queue for the next open (returns None)."""
        blocked, reason = self.tracker.orders_blocked()
        if blocked:
            if ticket.id is not None:
                self.tracker.update_ticket_status(ticket.id, TicketStatus.BLOCKED)
            return None
        if ticket.id is None:
            raise ValueError("Ticket must be persisted (id) before execution")
        side = _side(ticket)
        self.tracker.update_ticket_status(ticket.id, TicketStatus.SUBMITTED)
        if self.fill_on == "next_open":
            self.pending.append(ticket)
            return None
        price = self._slipped(self.market_data.last_price(ticket.ticker), side)
        fill_id = self.tracker.insert_fill(
            ticket_id=ticket.id,
            ticker=ticket.ticker,
            side=side,
            qty=float(ticket.qty),
            price=float(price),
            mode="dry_run",
        )
        cash = float(self.tracker.get_state("cash", "100"))
        positions = self.tracker.list_positions()
        equity = cash
        for p in positions:
            try:
                px = self.market_data.last_price(p["ticker"])
            except Exception:  # noqa: BLE001
                px = float(p["avg_cost"])
            equity += float(p["qty"]) * px
        peak = max(float(self.tracker.get_state("peak_equity", "100")), equity)
        day_start = float(self.tracker.get_state("day_start_equity", "100"))
        self.tracker.snapshot_portfolio(
            equity=equity,
            cash=cash,
            positions=positions,
            daily_pnl=equity - day_start,
            drawdown=(peak - equity) / peak if peak else 0.0,
            peak_equity=peak,
        )
        self.tracker.set_state("equity", f"{equity:.6f}")
        self.tracker.set_state("peak_equity", f"{peak:.6f}")
        result = {
            "fill_id": fill_id,
            "ticker": ticket.ticker,
            "side": side,
            "qty": float(ticket.qty),
            "price": float(price),
            "mode": "dry_run",
            "notional": abs(float(ticket.qty) * float(price)),
        }
        return result

    def _cancel(self, ticket: TradeTicket, reason: str) -> dict[str, Any]:
        if ticket.id is not None:
            self.tracker.update_ticket_status(ticket.id, TicketStatus.CANCELLED)
        return {
            "ticket_id": ticket.id,
            "ticker": ticket.ticker,
            "side": _side(ticket),
            "qty": float(ticket.qty),
            "reason": reason,
            "as_of": str(self.market_data.as_of.date()) if self.market_data.as_of is not None else None,
        }

    def fill_pending(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Fill queued tickets at the as-of session open. Returns (fills, cancelled).

        Sells go first so their proceeds can fund buys; a buy larger than cash is
        trimmed to fit. Equity is not re-marked here — the sleeve marks at close.
        """
        queue = sorted(self.pending, key=lambda t: 0 if _side(t) == "sell" else 1)
        self.pending.clear()
        fills: list[dict[str, Any]] = []
        cancelled: list[dict[str, Any]] = []
        for ticket in queue:
            side = _side(ticket)
            px = self.market_data.session_open(ticket.ticker)
            if px is None:
                cancelled.append(self._cancel(ticket, "no bar at next session"))
                continue
            price = self._slipped(px, side)
            qty = float(ticket.qty)
            if side == "buy":
                cash = float(self.tracker.get_state("cash", "0"))
                qty = min(qty, max(cash, 0.0) / price)
            else:
                held = {p["ticker"]: float(p["qty"]) for p in self.tracker.list_positions()}
                qty = min(qty, held.get(ticket.ticker.upper(), 0.0))
            if qty <= 1e-9:
                reason = "no cash at fill" if side == "buy" else "no position at fill"
                cancelled.append(self._cancel(ticket, reason))
                continue
            fill_id = self.tracker.insert_fill(
                ticket_id=ticket.id,
                ticker=ticket.ticker,
                side=side,
                qty=qty,
                price=float(price),
                mode="dry_run",
            )
            fills.append(
                {
                    "fill_id": fill_id,
                    "ticker": ticket.ticker,
                    "side": side,
                    "qty": qty,
                    "price": float(price),
                    "mode": "dry_run",
                    "notional": abs(qty * float(price)),
                    "trimmed": qty < float(ticket.qty) - 1e-9,
                }
            )
        return fills, cancelled

    def cancel_pending(self, reason: str) -> list[dict[str, Any]]:
        cancelled = [self._cancel(t, reason) for t in self.pending]
        self.pending.clear()
        return cancelled


def _seed_sleeve_tracker(db_path: str, cash: float) -> Tracker:
    tr = Tracker(db_path)
    tr.set_state("cash", f"{cash:.6f}")
    tr.set_state("equity", f"{cash:.6f}")
    tr.set_state("peak_equity", f"{cash:.6f}")
    tr.set_state("day_start_equity", f"{cash:.6f}")
    tr.snapshot_portfolio(
        equity=cash,
        cash=cash,
        positions=[],
        daily_pnl=0.0,
        drawdown=0.0,
        peak_equity=cash,
    )
    return tr


def _mark_sleeve(sleeve: SleeveState, md: AsOfMarketData) -> float:
    cash = float(sleeve.tracker.get_state("cash", "100"))
    equity = cash
    for p in sleeve.tracker.list_positions():
        try:
            px = md.last_price(p["ticker"])
        except Exception:  # noqa: BLE001
            px = float(p["avg_cost"])
        equity += float(p["qty"]) * px
    peak = max(float(sleeve.tracker.get_state("peak_equity", str(equity))), equity)
    day_start = float(sleeve.tracker.get_state("day_start_equity", str(equity)))
    positions = sleeve.tracker.list_positions()
    sleeve.tracker.snapshot_portfolio(
        equity=equity,
        cash=cash,
        positions=positions,
        daily_pnl=equity - day_start,
        drawdown=(peak - equity) / peak if peak else 0.0,
        peak_equity=peak,
    )
    sleeve.tracker.set_state("equity", f"{equity:.6f}")
    sleeve.tracker.set_state("peak_equity", f"{peak:.6f}")
    return float(equity)


def _clear_day_halt(tracker: Tracker) -> None:
    """Allow a new trading day after a prior day-loss halt; keep full halt if set."""
    if tracker.is_halted() and not tracker.is_day_halted():
        return
    if tracker.is_day_halted():
        tracker.resume("backtest: new trading day")


def _run_sleeve_day(
    sleeve: SleeveState,
    md: AsOfMarketData,
    cfg: AppConfig,
    *,
    slippage_bps: float,
    fill_on: str,
    fundamentals_cache_dir: Path | str | None = None,
    fundamentals_bundles: dict[str, dict] | None = None,
) -> None:
    _clear_day_halt(sleeve.tracker)
    # Reset day-start equity for daily loss checks
    eq_now = float(sleeve.tracker.get_state("equity", "100"))
    sleeve.tracker.set_state("day_start_equity", f"{eq_now:.6f}")

    ex = SimExecutionAgent(
        sleeve.tracker, md, slippage_bps=slippage_bps, fill_on=fill_on, pending=sleeve.pending
    )
    # Orders queued at yesterday's close fill at today's open, before today's signals.
    if sleeve.pending:
        opened, cancelled = ex.fill_pending()
        for result in opened:
            _record_fill(sleeve, result)
        sleeve.cancelled.extend(cancelled)

    agents = build_backtest_agents(
        md,
        universe=sleeve.tickers,
        fundamentals_cache_dir=fundamentals_cache_dir,
        fundamentals_bundles=fundamentals_bundles,
        flow_overlays=cfg.risk.momentum_flow_overlays,
    )
    pm = PortfolioManager(sleeve.tracker, md, cfg)
    benchmark = cfg.universe.benchmark
    stored: list[tuple[int, Any]] = []

    for ticker in sleeve.tickers:
        for agent in agents:
            try:
                sig = agent.analyze(ticker, benchmark=benchmark)
                sid = sleeve.tracker.insert_signal(sig)
                stored.append((sid, sig))
            except Exception as exc:  # noqa: BLE001
                sleeve.errors.append(f"{agent.agent_id}/{ticker}: {exc}")

    decisions = pm.weigh_and_decide(stored)
    for d in decisions:
        if not isinstance(d, TradeTicket):
            continue
        try:
            result = ex.execute(d)
        except Exception as exc:  # noqa: BLE001
            sleeve.errors.append(f"execution/{d.ticker}: {exc}")
            continue
        if result:
            _record_fill(sleeve, result)

    equity = _mark_sleeve(sleeve, md)
    sleeve.equity_curve.append(equity)


def _record_fill(sleeve: SleeveState, result: dict[str, Any]) -> None:
    sleeve.fills.append(result)
    sleeve.trade_count += 1
    sleeve.turnover_notional += float(result.get("notional") or 0.0)


def _apply_backtest_risk_overrides(
    cfg: AppConfig,
    *,
    min_confidence_to_trade: float | None = None,
    min_confidence_to_sell: float | None = None,
    min_abs_score: float | None = None,
) -> AppConfig:
    """Return a copy of cfg with optional risk knobs (never mutates live YAML)."""
    # The live loss-from-start stop is account policy, not part of a strategy test.
    risk_updates: dict[str, float | None] = {"max_loss_from_start_pct": None}
    if min_confidence_to_trade is not None:
        risk_updates["min_confidence_to_trade"] = float(min_confidence_to_trade)
    if min_confidence_to_sell is not None:
        risk_updates["min_confidence_to_sell"] = float(min_confidence_to_sell)
    if min_abs_score is not None:
        risk_updates["min_abs_score"] = float(min_abs_score)
    new_risk = cfg.risk.model_copy(update=risk_updates)
    return cfg.model_copy(update={"risk": new_risk})


def run_theme_backtest(
    *,
    start: str,
    end: str,
    cash_per_sleeve: float = DEFAULT_CASH_PER_SLEEVE,
    config: AppConfig | None = None,
    cache_dir: Path | str | None = None,
    fundamentals_cache_dir: Path | str | None = None,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    fill_on: str = DEFAULT_FILL_ON,
    force_download: bool = False,
    bars: dict[str, pd.DataFrame] | None = None,
    themes: list[str] | None = None,
    output_dir: Path | str | None = None,
    work_dir: Path | str | None = None,
    min_confidence_to_trade: float | None = None,
    min_confidence_to_sell: float | None = None,
    min_abs_score: float | None = None,
) -> dict[str, Any]:
    """Replay each theme sleeve over [start, end] on as-of daily bars.

    If ``bars`` is provided, skips network download (used by unit tests).
    Confidence / score floors are backtest-only overrides on a config copy —
    they never rewrite ``config/risk.yaml`` or live ``run-once`` defaults.
    """
    from quant_system.backtest.fundamentals_asof import (
        APPROXIMATION_DOC,
        default_fundamentals_cache_dir,
        ensure_fundamentals,
    )

    if fill_on not in FILL_MODES:
        raise ValueError(f"fill_on must be one of {FILL_MODES}, got {fill_on!r}")
    base_cfg = config or load_config()
    cfg = _apply_backtest_risk_overrides(
        base_cfg,
        min_confidence_to_trade=min_confidence_to_trade,
        min_confidence_to_sell=min_confidence_to_sell,
        min_abs_score=min_abs_score,
    )
    cdir = Path(cache_dir) if cache_dir else default_cache_dir()
    fdir = Path(fundamentals_cache_dir) if fundamentals_cache_dir else default_fundamentals_cache_dir()
    theme_names = themes or list(cfg.universe.themes.keys())
    if not theme_names:
        raise ValueError("No themes configured")

    # Collect tickers: per-theme active + benchmark + macro proxies
    all_tickers: list[str] = [cfg.universe.benchmark]
    all_tickers.extend(MACRO_PROXY_TICKERS)
    theme_tickers: dict[str, list[str]] = {}
    for name in theme_names:
        members = cfg.universe.themes.get(name)
        if members is None:
            raise ValueError(f"Unknown theme: {name}")
        tickers = list(members.active)
        theme_tickers[name] = tickers
        all_tickers.extend(tickers)

    # Dedupe preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for t in all_tickers:
        u = t.upper()
        if u not in seen:
            seen.add(u)
            uniq.append(u)

    if bars is None:
        # Force dry-run / no broker side effects during download either
        loaded = ensure_bars(
            uniq, start=start, end=end, cache_dir=cdir, force=force_download
        )
    else:
        loaded = {k.upper(): v for k, v in bars.items()}

    # Warm as-of fundamentals cache (FMP when allowed, else yfinance statements).
    # When ``bars`` is injected (unit tests), never hit the network — empty bundles
    # yield honest insufficient-data neutrals from FundamentalAgent.
    fund_tickers = sorted({t for tlist in theme_tickers.values() for t in tlist})
    fund_bundles: dict[str, dict] = {}
    if bars is None:
        fund_bundles = ensure_fundamentals(
            fund_tickers, cache_dir=fdir, force=force_download
        )

    md = AsOfMarketData(loaded)
    days = md.trading_days(start, end, reference=cfg.universe.benchmark)
    if not days:
        # Fallback calendar from any theme ticker
        for tlist in theme_tickers.values():
            for t in tlist:
                days = md.trading_days(start, end, reference=t)
                if days:
                    break
            if days:
                break
    if not days:
        raise ValueError(f"No trading days in [{start}, {end}] with available bars")

    tmp_root = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="theme_bt_"))
    tmp_root.mkdir(parents=True, exist_ok=True)

    sleeves: list[SleeveState] = []
    for name in theme_names:
        db_path = str(tmp_root / f"{name}.db")
        tr = _seed_sleeve_tracker(db_path, float(cash_per_sleeve))
        sleeves.append(
            SleeveState(theme=name, tickers=list(theme_tickers[name]), tracker=tr)
        )

    # Isolate from live Alpaca even if .env is loaded
    prev_keys = {
        k: os.environ.pop(k, None)
        for k in (
            "ALPACA_API_KEY",
            "ALPACA_SECRET_KEY",
            "APCA_API_KEY_ID",
            "APCA_API_SECRET_KEY",
            "ALPACA_API_SECRET",
        )
    }
    os.environ["QUANT_SKIP_DOTENV"] = "1"

    try:
        for day in days:
            md.set_as_of(day)
            for sleeve in sleeves:
                _run_sleeve_day(
                    sleeve,
                    md,
                    cfg,
                    slippage_bps=slippage_bps,
                    fill_on=fill_on,
                    fundamentals_cache_dir=fdir,
                    fundamentals_bundles=fund_bundles,
                )
        for sleeve in sleeves:
            ex = SimExecutionAgent(sleeve.tracker, md, fill_on=fill_on, pending=sleeve.pending)
            sleeve.cancelled.extend(ex.cancel_pending("still queued after last day"))
    finally:
        for k, v in prev_keys.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    bench_kwargs = {"cash": float(cash_per_sleeve), "slippage_bps": slippage_bps, "fill_on": fill_on}
    spy_bench = buy_and_hold_curve(loaded, [cfg.universe.benchmark], days, **bench_kwargs)
    spy_stats = curve_stats(spy_bench["equity_curve"])

    summaries: list[dict[str, Any]] = []
    for sleeve in sleeves:
        ending_equity = (
            sleeve.equity_curve[-1]
            if sleeve.equity_curve
            else float(cash_per_sleeve)
        )
        ending_cash = float(sleeve.tracker.get_state("cash", str(cash_per_sleeve)))
        summaries.append(
            sleeve_summary(
                theme=sleeve.theme,
                starting_cash=float(cash_per_sleeve),
                ending_equity=ending_equity,
                ending_cash=ending_cash,
                equity_curve=sleeve.equity_curve,
                trade_count=sleeve.trade_count,
                turnover_notional=sleeve.turnover_notional,
                positions=sleeve.tracker.list_positions(),
                fills=sleeve.fills,
            )
        )
        ew_bench = buy_and_hold_curve(loaded, sleeve.tickers, days, **bench_kwargs)
        bench_stats = {
            "spy": {**spy_stats, "tickers": spy_bench["tickers"], "entry_date": spy_bench["entry_date"]},
            "equal_weight": {
                **curve_stats(ew_bench["equity_curve"]),
                "tickers": ew_bench["tickers"],
                "excluded": ew_bench["excluded"],
                "entry_date": ew_bench["entry_date"],
            },
        }
        summary = summaries[-1]
        summary["benchmarks"] = bench_stats
        summary.update(benchmark_comparison(summary, bench_stats))
        summary["cancelled_orders"] = len(sleeve.cancelled)
        summary["cancelled"] = sleeve.cancelled
        sleeve.tracker.close()

    leaderboard = rank_leaderboard(summaries)
    result: dict[str, Any] = {
        "meta": {
            "start": str(pd.Timestamp(start).date()),
            "end": str(pd.Timestamp(end).date()),
            "trading_days": len(days),
            "cash_per_sleeve": float(cash_per_sleeve),
            "slippage_bps": float(slippage_bps),
            "fill_model": FILL_MODEL_DOCS[fill_on],
            "fill_on": fill_on,
            "benchmark_model": BENCHMARK_DOC,
            "agents": dict(AGENT_STATUS),
            "cache_dir": str(cdir),
            "fundamentals_cache_dir": str(fdir),
            "fundamentals_asof": APPROXIMATION_DOC,
            "fundamentals_tickers_cached": sorted(fund_bundles.keys()),
            "risk_overrides": {
                "min_confidence_to_trade": float(cfg.risk.min_confidence_to_trade),
                "min_confidence_to_sell": float(cfg.risk.min_confidence_to_sell),
                "min_abs_score": float(getattr(cfg.risk, "min_abs_score", 0.05)),
                "live_defaults": {
                    "min_confidence_to_trade": float(base_cfg.risk.min_confidence_to_trade),
                    "min_confidence_to_sell": float(base_cfg.risk.min_confidence_to_sell),
                    "min_abs_score": float(getattr(base_cfg.risk, "min_abs_score", 0.05)),
                },
            },
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "paper_only": True,
        },
        "leaderboard": leaderboard,
        "sleeves": {s["theme"]: s for s in summaries},
    }

    if output_dir is not None:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        stamp = f"{pd.Timestamp(start).date()}_{pd.Timestamp(end).date()}"
        path = out / f"theme_leaderboard_{stamp}.json"
        path.write_text(json.dumps(result, indent=2, default=str) + "\n")
        latest = out / "theme_leaderboard_latest.json"
        latest.write_text(json.dumps(result, indent=2, default=str) + "\n")
        result["meta"]["output_path"] = str(path)
        result["meta"]["latest_path"] = str(latest)

    return result


def format_leaderboard_table(result: dict[str, Any]) -> str:
    rows = result.get("leaderboard") or []
    header = (
        f"{'Rank':<5} {'Theme':<12} {'PnL':>9} {'Return':>8} {'Sharpe':>7} {'MaxDD':>7} "
        f"{'Trades':>7} {'Turnover':>9} {'vs SPY':>8} {'vs EW':>8} {'Beats':>6}"
    )
    lines = [header, "-" * len(header)]
    for r in rows:
        vs = r.get("vs_benchmarks") or {}

        def _excess(name: str) -> str:
            v = vs.get(name)
            return f"{float(v['excess_return']):>+8.1%}" if v else f"{'n/a':>8}"

        lines.append(
            f"{r.get('rank', 0):<5} {r.get('theme', ''):<12} "
            f"{float(r.get('realized_pnl', 0)):>+9.2f} "
            f"{float(r.get('return_pct', 0)):>+8.1%} "
            f"{float(r.get('sharpe', 0)):>7.2f} "
            f"{float(r.get('max_dd', 0)):>7.1%} "
            f"{int(r.get('trade_count', 0)):>7} "
            f"{float(r.get('turnover', 0)):>9.2f} "
            f"{_excess('spy')} {_excess('equal_weight')} "
            f"{('yes' if r.get('beats_benchmarks') else 'no'):>6}"
        )

    meta = result.get("meta") or {}
    sleeves = result.get("sleeves") or {}
    spy = next(
        (s["benchmarks"]["spy"] for s in sleeves.values() if s.get("benchmarks")), None
    )
    lines.append("")
    if spy is not None:
        lines.append(
            f"SPY buy-and-hold: {float(spy['return_pct']):+.1%}, Sharpe {float(spy['sharpe']):.2f}, "
            f"max DD {float(spy['max_dd']):.1%}. vs EW = vs equal-weight buy-and-hold of the "
            "theme. Beats = Sharpe above both and higher CAGR than both."
        )
    lines.append(
        f"Fill: {meta.get('fill_on', '?')}, {float(meta.get('slippage_bps', 0)):g} bps slippage; "
        f"{meta.get('trading_days', '?')} trading days {meta.get('start', '?')} to {meta.get('end', '?')}"
    )
    ro = meta.get("risk_overrides") or {}
    live = ro.get("live_defaults") or {}
    if ro:
        lines.append(
            "Backtest-only floors (live in parentheses): "
            f"buy confidence {ro.get('min_confidence_to_trade', 0):g} ({live.get('min_confidence_to_trade', 0):g}), "
            f"sell confidence {ro.get('min_confidence_to_sell', 0):g} ({live.get('min_confidence_to_sell', 0):g}), "
            f"|score| {ro.get('min_abs_score', 0):g} ({live.get('min_abs_score', 0):g})"
        )
    return "\n".join(lines)
