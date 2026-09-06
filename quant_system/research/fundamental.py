"""FundamentalAgent — quality + valuation from free yfinance fundamentals."""

from __future__ import annotations

from typing import Any, Callable

from quant_system.market_data.interface import MarketData
from quant_system.models.signal import Direction, Signal
from quant_system.research.base import ResearchAgent

FundamentalsFetcher = Callable[[str], dict[str, Any]]

# Keys we try to pull from yfinance `.info` (gracefully skip missing).
_INFO_KEYS: tuple[str, ...] = (
    "revenueGrowth",
    "earningsGrowth",
    "earningsQuarterlyGrowth",
    "profitMargins",
    "operatingMargins",
    "grossMargins",
    "freeCashflow",
    "operatingCashflow",
    "debtToEquity",
    "trailingPE",
    "forwardPE",
    "priceToSalesTrailing12Months",
    "currentRatio",
    "quickRatio",
    "totalCash",
    "totalDebt",
    "totalRevenue",
    "returnOnEquity",
    "returnOnAssets",
    "pegRatio",
    "enterpriseToEbitda",
    "marketCap",
)


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def fetch_yfinance_fundamentals(ticker: str) -> dict[str, Any]:
    """Pull fundamentals via yfinance info. Returns {} on any failure."""
    try:
        import yfinance as yf

        info = yf.Ticker(ticker).info or {}
    except Exception:  # noqa: BLE001 — network / API flakiness
        return {}

    if not isinstance(info, dict) or not info:
        return {}

    out: dict[str, Any] = {}
    for key in _INFO_KEYS:
        val = _safe_float(info.get(key))
        if val is not None:
            out[key] = val
    # Require at least one real metric besides marketCap alone.
    meaningful = {k for k in out if k != "marketCap"}
    if not meaningful:
        return {}
    return out


def _score_fundamentals(metrics: dict[str, Any]) -> dict[str, Any]:
    """Map available metrics -> direction / confidence / quality / valuation notes."""
    if not metrics:
        return {
            "direction": Direction.NEUTRAL,
            "confidence": 15.0,
            "quality_score": 50.0,
            "valuation_note": "insufficient data",
            "raw_score": 0.0,
            "reasons": [
                "Insufficient fundamental data available; emitting neutral with low confidence."
            ],
            "metrics_used": {},
        }

    score = 0.0
    reasons: list[str] = []
    quality_pts = 0.0
    quality_n = 0
    metrics_used: dict[str, Any] = {}

    def take(key: str) -> float | None:
        v = _safe_float(metrics.get(key))
        if v is not None:
            metrics_used[key] = v
        return v

    # --- Growth ---
    rev_g = take("revenueGrowth")
    if rev_g is not None:
        quality_n += 1
        if rev_g >= 0.20:
            score += 18
            quality_pts += 90
            reasons.append(f"Strong revenue growth ({rev_g:.1%})")
        elif rev_g >= 0.05:
            score += 10
            quality_pts += 70
            reasons.append(f"Solid revenue growth ({rev_g:.1%})")
        elif rev_g >= 0:
            score += 3
            quality_pts += 55
            reasons.append(f"Flat/low revenue growth ({rev_g:.1%})")
        elif rev_g >= -0.10:
            score -= 8
            quality_pts += 35
            reasons.append(f"Mild revenue decline ({rev_g:.1%})")
        else:
            score -= 15
            quality_pts += 15
            reasons.append(f"Sharp revenue decline ({rev_g:.1%})")

    earn_g = take("earningsGrowth")
    if earn_g is None:
        earn_g = take("earningsQuarterlyGrowth")
    if earn_g is not None:
        quality_n += 1
        if earn_g >= 0.20:
            score += 15
            quality_pts += 90
            reasons.append(f"Strong earnings growth ({earn_g:.1%})")
        elif earn_g >= 0.05:
            score += 8
            quality_pts += 70
            reasons.append(f"Positive earnings growth ({earn_g:.1%})")
        elif earn_g >= 0:
            score += 2
            quality_pts += 55
            reasons.append(f"Flat earnings growth ({earn_g:.1%})")
        else:
            score -= 12
            quality_pts += 25
            reasons.append(f"Negative earnings growth ({earn_g:.1%})")

    # --- Margins ---
    profit_m = take("profitMargins")
    if profit_m is not None:
        quality_n += 1
        if profit_m >= 0.20:
            score += 12
            quality_pts += 90
            reasons.append(f"High profit margin ({profit_m:.1%})")
        elif profit_m >= 0.08:
            score += 6
            quality_pts += 70
            reasons.append(f"Healthy profit margin ({profit_m:.1%})")
        elif profit_m >= 0:
            score += 0
            quality_pts += 50
            reasons.append(f"Thin profit margin ({profit_m:.1%})")
        else:
            score -= 12
            quality_pts += 20
            reasons.append(f"Negative profit margin ({profit_m:.1%})")

    op_m = take("operatingMargins")
    if op_m is not None:
        quality_n += 1
        if op_m >= 0.15:
            score += 8
            quality_pts += 85
            reasons.append(f"Strong operating margin ({op_m:.1%})")
        elif op_m >= 0.05:
            score += 4
            quality_pts += 65
            reasons.append(f"OK operating margin ({op_m:.1%})")
        elif op_m >= 0:
            quality_pts += 45
            reasons.append(f"Thin operating margin ({op_m:.1%})")
        else:
            score -= 10
            quality_pts += 15
            reasons.append(f"Negative operating margin ({op_m:.1%})")

    # --- Free cash flow ---
    fcf = take("freeCashflow")
    rev = take("totalRevenue")
    if fcf is not None:
        quality_n += 1
        fcf_yield_proxy = None
        if rev and rev > 0:
            fcf_yield_proxy = fcf / rev
            metrics_used["fcf_to_revenue"] = fcf_yield_proxy
        if fcf > 0:
            score += 10
            quality_pts += 80
            note = f"Positive FCF ({fcf:,.0f})"
            if fcf_yield_proxy is not None:
                note += f"; FCF/rev {fcf_yield_proxy:.1%}"
            reasons.append(note)
        else:
            score -= 12
            quality_pts += 20
            reasons.append(f"Negative FCF ({fcf:,.0f}) — cash burn risk")

    # --- Balance sheet / leverage ---
    dte = take("debtToEquity")
    if dte is not None:
        quality_n += 1
        # yfinance often reports D/E as percent-like (e.g. 188.97 = 1.89x)
        dte_x = dte / 100.0 if dte > 10 else dte
        metrics_used["debt_to_equity_x"] = dte_x
        if dte_x <= 0.5:
            score += 8
            quality_pts += 85
            reasons.append(f"Low leverage (D/E {dte_x:.2f}x)")
        elif dte_x <= 1.5:
            score += 2
            quality_pts += 60
            reasons.append(f"Moderate leverage (D/E {dte_x:.2f}x)")
        elif dte_x <= 3.0:
            score -= 6
            quality_pts += 35
            reasons.append(f"Elevated leverage (D/E {dte_x:.2f}x)")
        else:
            score -= 12
            quality_pts += 15
            reasons.append(f"High leverage (D/E {dte_x:.2f}x)")

    current = take("currentRatio")
    if current is not None:
        quality_n += 1
        if current >= 1.5:
            score += 6
            quality_pts += 80
            reasons.append(f"Strong current ratio ({current:.2f})")
        elif current >= 1.0:
            score += 2
            quality_pts += 60
            reasons.append(f"Adequate current ratio ({current:.2f})")
        else:
            score -= 8
            quality_pts += 25
            reasons.append(f"Weak current ratio ({current:.2f})")

    roe = take("returnOnEquity")
    if roe is not None:
        quality_n += 1
        if roe >= 0.20:
            score += 8
            quality_pts += 90
            reasons.append(f"High ROE ({roe:.1%})")
        elif roe >= 0.08:
            score += 4
            quality_pts += 70
            reasons.append(f"Decent ROE ({roe:.1%})")
        elif roe >= 0:
            quality_pts += 45
            reasons.append(f"Low ROE ({roe:.1%})")
        else:
            score -= 8
            quality_pts += 20
            reasons.append(f"Negative ROE ({roe:.1%})")

    # --- Valuation ---
    pe = take("trailingPE")
    if pe is None:
        pe = take("forwardPE")
        if pe is not None and pe < 0:
            # Negative forward PE (loss-making) — treat as expensive/speculative
            score -= 8
            reasons.append(f"Negative forward P/E ({pe:.1f}) — unprofitable")
            pe = None  # don't use for cheap/expensive bands

    valuation_bits: list[str] = []
    if pe is not None and pe > 0:
        if pe < 15:
            score += 10
            valuation_bits.append(f"attractive P/E {pe:.1f}")
        elif pe < 25:
            score += 3
            valuation_bits.append(f"reasonable P/E {pe:.1f}")
        elif pe < 40:
            score -= 5
            valuation_bits.append(f"elevated P/E {pe:.1f}")
        else:
            score -= 12
            valuation_bits.append(f"rich P/E {pe:.1f}")

    ps = take("priceToSalesTrailing12Months")
    if ps is not None and ps > 0:
        if ps < 3:
            score += 6
            valuation_bits.append(f"attractive P/S {ps:.1f}")
        elif ps < 8:
            score += 1
            valuation_bits.append(f"moderate P/S {ps:.1f}")
        elif ps < 20:
            score -= 6
            valuation_bits.append(f"elevated P/S {ps:.1f}")
        else:
            score -= 12
            valuation_bits.append(f"very rich P/S {ps:.1f}")

    peg = take("pegRatio")
    if peg is not None and peg > 0:
        if peg < 1.0:
            score += 6
            valuation_bits.append(f"PEG {peg:.2f} (growth-cheap)")
        elif peg < 2.0:
            valuation_bits.append(f"PEG {peg:.2f}")
        else:
            score -= 4
            valuation_bits.append(f"PEG {peg:.2f} (growth-expensive)")

    if valuation_bits:
        valuation_note = "; ".join(valuation_bits)
    elif metrics_used:
        valuation_note = "valuation multiples unavailable"
    else:
        valuation_note = "insufficient data"

    quality_score = float(quality_pts / quality_n) if quality_n else 50.0
    metrics_used["quality_inputs"] = quality_n

    # Coverage penalty: thin data -> lower confidence / pull toward neutral
    coverage = len(metrics_used) - 1  # exclude quality_inputs
    if coverage < 3:
        score *= 0.4
        reasons.append(f"Sparse fundamentals coverage ({coverage} fields)")

    if score >= 15:
        direction = Direction.LONG
    elif score <= -15:
        direction = Direction.SHORT
    else:
        direction = Direction.NEUTRAL

    # Confidence: |score| + coverage, clipped
    conf = 25.0 + abs(score) * 0.9 + min(coverage, 10) * 2.5
    if coverage < 3:
        conf = min(conf, 35.0)
    confidence = float(max(5.0, min(92.0, conf)))

    if not reasons:
        reasons.append("Fundamentals present but inconclusive")

    return {
        "direction": direction,
        "confidence": confidence,
        "quality_score": round(quality_score, 1),
        "valuation_note": valuation_note,
        "raw_score": round(score, 2),
        "reasons": reasons,
        "metrics_used": metrics_used,
        "coverage": coverage,
    }


class FundamentalAgent(ResearchAgent):
    """Quality + valuation signals from free yfinance fundamentals.

    Gracefully handles missing fields: neutral + low confidence, never crashes.
    """

    agent_id = "fundamental"

    def __init__(
        self,
        market_data: MarketData,
        fundamentals_fetcher: FundamentalsFetcher | None = None,
    ):
        super().__init__(market_data)
        self._fetch = fundamentals_fetcher or fetch_yfinance_fundamentals

    def analyze(self, ticker: str, *, benchmark: str = "SPY") -> Signal:
        fetch_error: str | None = None
        try:
            metrics = dict(self._fetch(ticker) or {})
        except Exception as exc:  # noqa: BLE001
            metrics = {}
            fetch_error = str(exc)

        result = _score_fundamentals(metrics)

        # Optional price snapshot for logs (never fail the signal on this).
        snap: dict[str, Any] | None = None
        try:
            snap = self.market_data.snapshot(ticker)
        except Exception as exc:  # noqa: BLE001
            snap = {"error": str(exc)}

        supporting: dict[str, Any] = {
            "metrics": result["metrics_used"],
            "quality_score": result["quality_score"],
            "valuation_note": result["valuation_note"],
            "raw_score": result["raw_score"],
            "coverage": result.get("coverage", 0),
            "benchmark": benchmark,
            "snapshot": snap,
            "fetch_error": fetch_error,
            "stub": False,
        }

        return Signal(
            agent_id=self.agent_id,
            ticker=ticker,
            direction=result["direction"],
            confidence=float(result["confidence"]),
            reasoning="; ".join(result["reasons"]),
            supporting_data=supporting,
        )
