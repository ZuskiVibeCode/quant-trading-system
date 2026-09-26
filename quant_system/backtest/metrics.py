"""Sleeve equity-curve metrics for theme leaderboard."""

from __future__ import annotations

from typing import Any


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
    return {
        "theme": theme,
        "starting_cash": float(starting_cash),
        "ending_cash": float(ending_cash),
        "ending_equity": float(ending_equity),
        "realized_pnl": realized_pnl,
        "return_pct": (realized_pnl / starting_cash) if starting_cash else 0.0,
        "max_dd": max_drawdown(equity_curve),
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
