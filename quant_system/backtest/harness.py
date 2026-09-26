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
from quant_system.backtest.cache import MACRO_PROXY_TICKERS, default_cache_dir, ensure_bars
from quant_system.backtest.metrics import rank_leaderboard, sleeve_summary
from quant_system.backtest.stubs import AGENT_STATUS, build_backtest_agents
from quant_system.config.loader import AppConfig, load_config
from quant_system.execution.portfolio_manager import PortfolioManager
from quant_system.models.ticket import TicketStatus, TradeTicket
from quant_system.tracker.api import Tracker

# Fill model (v1): same-bar close with constant bps slippage; instant full fills.
# Buy pays close * (1 + slip); sell receives close * (1 - slip).
DEFAULT_SLIPPAGE_BPS = 5.0
DEFAULT_CASH_PER_SLEEVE = 100.0

FILL_MODEL_DOC = (
    "same-bar close + constant slippage_bps (default 5); "
    "buy pays close*(1+slip), sell receives close*(1-slip); "
    "instant full fills; no partials / no latency"
)


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


class SimExecutionAgent:
    """Dry-run fills only (never Alpaca). Applies constant bps slippage to last_price."""

    def __init__(
        self,
        tracker: Tracker,
        market_data: AsOfMarketData,
        *,
        slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
        fill_on: str = "close",
    ):
        self.tracker = tracker
        self.market_data = market_data
        self.slippage_bps = float(slippage_bps)
        self.fill_on = fill_on  # "close" (v1) or "next_open"
        self.mode = "backtest_sim"

    def _fill_price(self, ticker: str, side: str) -> float:
        slip = self.slippage_bps / 10_000.0
        if self.fill_on == "next_open":
            px = self.market_data.next_open(ticker)
            if px is None:
                px = self.market_data.open_price(ticker)
        else:
            px = self.market_data.last_price(ticker)
        if side == "buy":
            return float(px) * (1.0 + slip)
        return float(px) * (1.0 - slip)

    def execute(self, ticket: TradeTicket) -> dict[str, Any] | None:
        blocked, reason = self.tracker.orders_blocked()
        if blocked:
            if ticket.id is not None:
                self.tracker.update_ticket_status(ticket.id, TicketStatus.BLOCKED)
            return None
        if ticket.id is None:
            raise ValueError("Ticket must be persisted (id) before execution")
        side = ticket.side.value if hasattr(ticket.side, "value") else str(ticket.side)
        price = self._fill_price(ticket.ticker, side)
        self.tracker.update_ticket_status(ticket.id, TicketStatus.SUBMITTED)
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
) -> None:
    _clear_day_halt(sleeve.tracker)
    # Reset day-start equity for daily loss checks
    eq_now = float(sleeve.tracker.get_state("equity", "100"))
    sleeve.tracker.set_state("day_start_equity", f"{eq_now:.6f}")

    agents = build_backtest_agents(md, universe=sleeve.tickers)
    pm = PortfolioManager(sleeve.tracker, md, cfg)
    ex = SimExecutionAgent(sleeve.tracker, md, slippage_bps=slippage_bps, fill_on=fill_on)
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
            sleeve.fills.append(result)
            sleeve.trade_count += 1
            sleeve.turnover_notional += float(result.get("notional") or 0.0)

    equity = _mark_sleeve(sleeve, md)
    sleeve.equity_curve.append(equity)


def run_theme_backtest(
    *,
    start: str,
    end: str,
    cash_per_sleeve: float = DEFAULT_CASH_PER_SLEEVE,
    config: AppConfig | None = None,
    cache_dir: Path | str | None = None,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    fill_on: str = "close",
    force_download: bool = False,
    bars: dict[str, pd.DataFrame] | None = None,
    themes: list[str] | None = None,
    output_dir: Path | str | None = None,
    work_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Replay each theme sleeve over [start, end] on as-of daily bars.

    If ``bars`` is provided, skips network download (used by unit tests).
    """
    cfg = config or load_config()
    cdir = Path(cache_dir) if cache_dir else default_cache_dir()
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
                )
    finally:
        for k, v in prev_keys.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

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
        sleeve.tracker.close()

    leaderboard = rank_leaderboard(summaries)
    result: dict[str, Any] = {
        "meta": {
            "start": str(pd.Timestamp(start).date()),
            "end": str(pd.Timestamp(end).date()),
            "trading_days": len(days),
            "cash_per_sleeve": float(cash_per_sleeve),
            "slippage_bps": float(slippage_bps),
            "fill_model": FILL_MODEL_DOC,
            "fill_on": fill_on,
            "agents": dict(AGENT_STATUS),
            "cache_dir": str(cdir),
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
    lines = [
        f"{'Rank':<5} {'Theme':<12} {'PnL':>10} {'Equity':>10} {'MaxDD':>8} {'Trades':>7} {'Turnover':>9}",
        "-" * 64,
    ]
    for r in rows:
        lines.append(
            f"{r.get('rank', 0):<5} {r.get('theme', ''):<12} "
            f"{float(r.get('realized_pnl', 0)):>+10.2f} "
            f"{float(r.get('ending_equity', 0)):>10.2f} "
            f"{float(r.get('max_dd', 0)):>7.1%} "
            f"{int(r.get('trade_count', 0)):>7} "
            f"{float(r.get('turnover', 0)):>9.2f}"
        )
    return "\n".join(lines)
