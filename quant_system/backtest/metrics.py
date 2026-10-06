"""Sleeve equity-curve metrics for theme leaderboard."""

from __future__ import annotations

import math
from typing import Any

TRADING_DAYS_PER_YEAR = 252


def daily_returns(equity_curve: list[float]) -> list[float]:
    """Simple close-to-close returns; skips steps from a non-positive value."""
    out: list[float] = []
    for prev, cur in zip(equity_curve, equity_curve[1:]):
        if prev > 0:
            out.append(cur / prev - 1.0)
    return out


def sharpe_ratio(equity_curve: list[float]) -> float:
    """Annualized Sharpe of daily returns (risk-free = 0). 0.0 when undefined."""
    rets = daily_returns(equity_curve)
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var <= 0:
        return 0.0
    return float(mean / math.sqrt(var) * math.sqrt(TRADING_DAYS_PER_YEAR))


def cagr(equity_curve: list[float]) -> float:
    """Annualized growth over the curve, treating each step as one trading day."""
    if len(equity_curve) < 2 or equity_curve[0] <= 0 or equity_curve[-1] <= 0:
        return 0.0
    years = (len(equity_curve) - 1) / TRADING_DAYS_PER_YEAR
    return float((equity_curve[-1] / equity_curve[0]) ** (1.0 / years) - 1.0)


def curve_stats(equity_curve: list[float]) -> dict[str, float]:
    """Return / CAGR / Sharpe / max drawdown for a curve whose first point is starting cash."""
    start = equity_curve[0] if equity_curve else 0.0
    end = equity_curve[-1] if equity_curve else 0.0
    return {
        "return_pct": (end / start - 1.0) if start else 0.0,
        "cagr": cagr(equity_curve),
        "sharpe": sharpe_ratio(equity_curve),
        "max_dd": max_drawdown(equity_curve),
    }


def benchmark_comparison(
    strategy: dict[str, float],
    benchmarks: dict[str, dict[str, float]],
) -> dict[str, Any]:
    """Compare strategy curve_stats to each benchmark's curve_stats.

    ``beats_benchmarks`` is the gauntlet pass bar: Sharpe above every benchmark's
    Sharpe and positive excess CAGR against every benchmark.
    """
    vs: dict[str, dict[str, float]] = {}
    for name, b in benchmarks.items():
        vs[name] = {
            "excess_return": float(strategy["return_pct"] - b["return_pct"]),
            "excess_cagr": float(strategy["cagr"] - b["cagr"]),
            "sharpe_diff": float(strategy["sharpe"] - b["sharpe"]),
        }
    beats = bool(vs) and all(v["sharpe_diff"] > 0 and v["excess_cagr"] > 0 for v in vs.values())
    return {"vs_benchmarks": vs, "beats_benchmarks": beats}


def max_drawdown(equity_curve: list[float]) -> float:
    """Max peak-to-trough drawdown as a fraction of peak (0..1)."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
    return float(max_dd)


def sleeve_summary(
    *,
    theme: str,
    starting_cash: float,
    ending_equity: float,
    ending_cash: float,
    equity_curve: list[float],
    trade_count: int,
    turnover_notional: float,
    positions: list[dict[str, Any]] | None = None,
    fills: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    realized_pnl = float(ending_equity) - float(starting_cash)
    avg_equity = (
        sum(equity_curve) / len(equity_curve) if equity_curve else float(starting_cash)
    )
    turnover = float(turnover_notional) / avg_equity if avg_equity else 0.0
    # Curves hold one mark per trading day; prepend starting cash for day-1 return.
    full_curve = [float(starting_cash)] + [float(x) for x in equity_curve]
    return {
        "theme": theme,
        "starting_cash": float(starting_cash),
        "ending_cash": float(ending_cash),
        "ending_equity": float(ending_equity),
        "realized_pnl": realized_pnl,
        "return_pct": (realized_pnl / starting_cash) if starting_cash else 0.0,
        "cagr": cagr(full_curve),
        "sharpe": sharpe_ratio(full_curve),
        "max_dd": max_drawdown(full_curve),
        "trade_count": int(trade_count),
        "turnover": turnover,
        "turnover_notional": float(turnover_notional),
        "final_positions": positions or [],
        "fill_count": len(fills or []),
    }


def rank_leaderboard(sleeves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank by realized_pnl desc, then ending_equity, then lower max_dd."""
    ranked = sorted(
        sleeves,
        key=lambda s: (
            -float(s.get("realized_pnl", 0.0)),
            -float(s.get("ending_equity", 0.0)),
            float(s.get("max_dd", 0.0)),
        ),
    )
    out: list[dict[str, Any]] = []
    for i, row in enumerate(ranked, start=1):
        item = dict(row)
        item["rank"] = i
        out.append(item)
    return out
