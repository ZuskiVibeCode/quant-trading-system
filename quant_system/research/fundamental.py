"""FundamentalAgent — quality + valuation desk overlay from free yfinance fundamentals."""

from __future__ import annotations

import re
from typing import Any, Callable

from quant_system.market_data.interface import MarketData
from quant_system.models.signal import Direction, Signal
from quant_system.research.base import ResearchAgent

FundamentalsFetcher = Callable[[str], dict[str, Any]]

# Numeric keys we try to pull from yfinance `.info` (gracefully skip missing).
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
    "enterpriseToRevenue",
    "enterpriseValue",
    "marketCap",
    "sharesOutstanding",
    "floatShares",
    "impliedSharesOutstanding",
    "currentPrice",
    "regularMarketPrice",
    "previousClose",
    "shortTermInvestments",
)

# Text fields scanned only for soft dilution language (never invent filings).
_TEXT_KEYS: tuple[str, ...] = (
    "longBusinessSummary",
    "description",
    "companyOfficers",  # ignored unless str; kept for future
)

_DILUTION_TEXT_RE = re.compile(
    r"\b(warrant|warrants|convertible|convertibles|dilut(?:e|ion|ive)|"
    r"preferred\s+stock|atm\s+offering|at[- ]the[- ]market)\b",
    re.IGNORECASE,
)

# Soft overlay thresholds (Momentum-like magnitude: roughly ±3..±10)
_EXTREME_PS = 20.0
_ELEVATED_PS = 8.0
_EXTREME_EV_SALES = 25.0
_ELEVATED_EV_SALES = 10.0
_SHARE_COUNT_GAP_PCT = 0.08  # 8% implied vs reported shares
_FLOAT_TIGHT_PCT = 0.55  # float < 55% of shares outstanding → soft flag


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

    # Optional text for dilution language scan (best-effort, never required).
    text_bits: list[str] = []
    for key in ("longBusinessSummary", "description"):
        raw = info.get(key)
        if isinstance(raw, str) and raw.strip():
            text_bits.append(raw.strip())
            out[key] = raw.strip()[:4000]
    if text_bits:
        out["_info_text"] = " ".join(text_bits)[:8000]

    # Require at least one real metric besides marketCap / price / text alone.
    skip = {
        "marketCap",
        "currentPrice",
        "regularMarketPrice",
        "previousClose",
        "longBusinessSummary",
        "description",
        "_info_text",
    }
    meaningful = {k for k in out if k not in skip}
    if not meaningful:
        return {}
    return out


def rebuild_capital_structure(metrics: dict[str, Any]) -> dict[str, Any]:
    """Rebuild market cap / cash / debt / EV when fields exist.

    Returns supporting_data-friendly keys: market_cap, cash, debt, ev, shares_out,
    plus labels when EV is estimated vs vendor-provided.
    """
    out: dict[str, Any] = {
        "market_cap": None,
        "cash": None,
        "debt": None,
        "ev": None,
        "shares_out": None,
        "ev_source": None,
        "net_cash": None,
    }

    market_cap = _safe_float(metrics.get("marketCap"))
    shares = _safe_float(metrics.get("sharesOutstanding"))
    total_cash = _safe_float(metrics.get("totalCash"))
    st_inv = _safe_float(metrics.get("shortTermInvestments"))
    debt = _safe_float(metrics.get("totalDebt"))
    vendor_ev = _safe_float(metrics.get("enterpriseValue"))

    cash: float | None = None
    if total_cash is not None and st_inv is not None and st_inv > 0:
        # Avoid double-count if totalCash already embeds ST investments (common).
        # Prefer totalCash alone when both present; note ST investments separately.
        cash = total_cash
        out["short_term_investments"] = st_inv
    elif total_cash is not None:
        cash = total_cash
    elif st_inv is not None:
        cash = st_inv

    out["market_cap"] = market_cap
    out["cash"] = cash
    out["debt"] = debt
    out["shares_out"] = shares

    if cash is not None and debt is not None:
        out["net_cash"] = cash - debt

    rebuilt_ev: float | None = None
    if market_cap is not None and (debt is not None or cash is not None):
        rebuilt_ev = market_cap + (debt or 0.0) - (cash or 0.0)

    if vendor_ev is not None and vendor_ev > 0:
        out["ev"] = vendor_ev
        out["ev_source"] = "yfinance"
        if rebuilt_ev is not None:
            out["ev_estimate"] = rebuilt_ev
    elif rebuilt_ev is not None:
        out["ev"] = rebuilt_ev
        out["ev_source"] = "estimate"
        out["ev_estimate"] = rebuilt_ev
    elif market_cap is not None:
        # Last resort: EV ≈ market cap when cash/debt missing (clearly labeled).
        out["ev"] = market_cap
        out["ev_source"] = "market_cap_proxy"
        out["ev_estimate"] = market_cap

    return out


def sales_multiples(
    metrics: dict[str, Any],
    capital: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Trailing revenue, growth, P/S and EV/sales when computable."""
    capital = capital or {}
    out: dict[str, Any] = {
        "revenue": None,
        "revenue_growth": None,
        "price_to_sales": None,
        "ev_to_sales": None,
        "multiple_extreme": False,
        "multiple_note": None,
    }

    rev = _safe_float(metrics.get("totalRevenue"))
    rev_g = _safe_float(metrics.get("revenueGrowth"))
    ps = _safe_float(metrics.get("priceToSalesTrailing12Months"))
    etr = _safe_float(metrics.get("enterpriseToRevenue"))

    market_cap = _safe_float(capital.get("market_cap")) or _safe_float(metrics.get("marketCap"))
    ev = _safe_float(capital.get("ev"))

    out["revenue"] = rev
    out["revenue_growth"] = rev_g

    if ps is None and market_cap is not None and rev is not None and rev > 0:
        ps = market_cap / rev
        out["price_to_sales_source"] = "market_cap/revenue"
    elif ps is not None:
        out["price_to_sales_source"] = "yfinance"

    if etr is None and ev is not None and rev is not None and rev > 0:
        etr = ev / rev
        out["ev_to_sales_source"] = "ev/revenue"
    elif etr is not None:
        out["ev_to_sales_source"] = "yfinance"

    out["price_to_sales"] = ps
    out["ev_to_sales"] = etr

    notes: list[str] = []
    extreme = False
    if etr is not None and etr > 0:
        if etr >= _EXTREME_EV_SALES:
            extreme = True
            notes.append(f"extreme EV/sales {etr:.1f}x (theme names often rich)")
        elif etr >= _ELEVATED_EV_SALES:
            notes.append(f"elevated EV/sales {etr:.1f}x")
    if ps is not None and ps > 0:
        if ps >= _EXTREME_PS:
            extreme = True
            notes.append(f"extreme P/S {ps:.1f}x (theme names often rich)")
        elif ps >= _ELEVATED_PS:
            notes.append(f"elevated P/S {ps:.1f}x")

    out["multiple_extreme"] = extreme
    out["multiple_note"] = "; ".join(notes) if notes else None
    return out


def dilution_soft_flags(metrics: dict[str, Any]) -> dict[str, Any]:
    """Soft dilution / share-count risk flags from yfinance fields only.

    Never invents filings, unlocks, or insider trades. Text scan is best-effort
    against info summary only when present.
    """
    out: dict[str, Any] = {
        "dilution_flag": False,
        "dilution_reasons": [],
        "shares_out": None,
        "float_shares": None,
        "implied_shares": None,
        "share_count_gap_pct": None,
        "float_to_shares": None,
        "text_dilution_hit": False,
    }

    shares = _safe_float(metrics.get("sharesOutstanding"))
    float_sh = _safe_float(metrics.get("floatShares"))
    implied_vendor = _safe_float(metrics.get("impliedSharesOutstanding"))
    market_cap = _safe_float(metrics.get("marketCap"))
    price = (
        _safe_float(metrics.get("currentPrice"))
        or _safe_float(metrics.get("regularMarketPrice"))
        or _safe_float(metrics.get("previousClose"))
    )

    out["shares_out"] = shares
    out["float_shares"] = float_sh

    implied: float | None = implied_vendor
    if implied is None and market_cap is not None and price is not None and price > 0:
        implied = market_cap / price
    out["implied_shares"] = implied

    reasons: list[str] = []

    if shares is not None and shares > 0 and implied is not None and implied > 0:
        gap = (implied - shares) / shares
        out["share_count_gap_pct"] = gap
        if abs(gap) >= _SHARE_COUNT_GAP_PCT:
            out["dilution_flag"] = True
            direction = "above" if gap > 0 else "below"
            reasons.append(
                f"Implied shares {direction} reported by {abs(gap):.1%} "
                f"(soft share-count mismatch)"
            )

    if shares is not None and shares > 0 and float_sh is not None and float_sh > 0:
        ratio = float_sh / shares
        out["float_to_shares"] = ratio
        if ratio < _FLOAT_TIGHT_PCT:
            out["dilution_flag"] = True
            reasons.append(
                f"Tight float ({ratio:.0%} of shares out) — overhang / unlock risk soft flag"
            )

    text = metrics.get("_info_text") or metrics.get("longBusinessSummary") or metrics.get("description")
    if isinstance(text, str) and text.strip():
        if _DILUTION_TEXT_RE.search(text):
            out["dilution_flag"] = True
            out["text_dilution_hit"] = True
            reasons.append(
                "Info text mentions warrants/convertibles/dilution language (soft; not a filing parse)"
            )

    out["dilution_reasons"] = reasons
    return out


def _apply_desk_overlays(
    score: float,
    reasons: list[str],
    *,
    capital: dict[str, Any],
    multiples: dict[str, Any],
    dilution: dict[str, Any],
    metrics: dict[str, Any],
) -> tuple[float, list[str], list[str]]:
    """Soft score bumps (Momentum-like ±3..±10). Returns score, reasons, bear_lines."""
    bear: list[str] = []

    # --- Capital structure soft notes ---
    net_cash = _safe_float(capital.get("net_cash"))
    market_cap = _safe_float(capital.get("market_cap"))
    if net_cash is not None and market_cap is not None and market_cap > 0:
        net_pct = net_cash / market_cap
        if net_pct >= 0.20:
            score += 5
            reasons.append(f"Net cash cushion (~{net_pct:.0%} of mkt cap)")
        elif net_pct <= -0.40:
            score -= 6
            msg = f"Net debt heavy (~{abs(net_pct):.0%} of mkt cap)"
            reasons.append(msg)
            bear.append(msg)

    # Cash burn already scored in quality; amplify bear line when FCF negative + thin cash
    fcf = _safe_float(metrics.get("freeCashflow"))
    cash = _safe_float(capital.get("cash"))
    if fcf is not None and fcf < 0:
        burn_line = f"Cash burn (FCF {fcf:,.0f})"
        if cash is not None and cash > 0 and abs(fcf) > 0:
            # crude runway quarters if burn annualized ≈ |fcf|
            quarters = cash / (abs(fcf) / 4.0) if abs(fcf) > 0 else None
            if quarters is not None and quarters < 8:
                burn_line += f"; cash runway soft-est. ~{quarters:.1f} qtrs"
                score -= 4
        bear.append(burn_line)

    # --- Sales multiple overlays ---
    etr = _safe_float(multiples.get("ev_to_sales"))
    ps = _safe_float(multiples.get("price_to_sales"))
    if etr is not None and etr > 0:
        if etr >= _EXTREME_EV_SALES:
            score -= 8
            msg = f"Rich EV/sales {etr:.1f}x — extreme for theme names"
            reasons.append(msg)
            bear.append(msg)
        elif etr >= _ELEVATED_EV_SALES:
            score -= 4
            reasons.append(f"Elevated EV/sales {etr:.1f}x")
        elif etr < 3:
            score += 4
            reasons.append(f"Attractive EV/sales {etr:.1f}x")
    elif ps is not None and ps > 0:
        if ps >= _EXTREME_PS:
            score -= 8
            msg = f"Rich P/S {ps:.1f}x — extreme for theme names"
            reasons.append(msg)
            bear.append(msg)
        elif ps >= _ELEVATED_PS:
            score -= 4
            reasons.append(f"Elevated P/S {ps:.1f}x")

    # Avoid double-counting: base scorer already touches P/S bands. Overlay above
    # only adds theme-extreme / EV path nuance; small extra bump already applied.

    # --- Dilution soft flags ---
    if dilution.get("dilution_flag"):
        n = len(dilution.get("dilution_reasons") or [])
        bump = -4 if n <= 1 else -7
        score += bump
        for r in dilution.get("dilution_reasons") or []:
            reasons.append(r)
            bear.append(r)

    return score, reasons, bear


def _format_desk_reasoning(
    *,
    quality_bits: list[str],
    valuation_note: str,
    capital: dict[str, Any],
    multiples: dict[str, Any],
    bear_lines: list[str],
    insufficient: bool,
    coverage: int,
) -> str:
    """Concise Orbit-friendly desk template."""
    parts: list[str] = []

    if insufficient:
        return (
            "Insufficient fundamental data available; emitting neutral with low confidence. "
            "Quality: n/a. Valuation/EV: insufficient data. "
            "Bear/risk: cannot assess without filings-grade fields (yfinance often sparse "
            "for small quantum names)."
        )

    # Quality snapshot
    if quality_bits:
        parts.append("Quality: " + "; ".join(quality_bits[:4]))
    else:
        parts.append("Quality: inconclusive / thin coverage")

    # Valuation / EV vs sales
    val_bits: list[str] = []
    if valuation_note and valuation_note not in {"insufficient data", "valuation multiples unavailable"}:
        val_bits.append(valuation_note)
    ev = capital.get("ev")
    ev_src = capital.get("ev_source")
    if ev is not None:
        label = "EV" if ev_src == "yfinance" else "EV (est.)"
        val_bits.append(f"{label} {ev:,.0f}")
    etr = multiples.get("ev_to_sales")
    if etr is not None:
        val_bits.append(f"EV/sales {etr:.1f}x")
    elif multiples.get("price_to_sales") is not None:
        val_bits.append(f"P/S {multiples['price_to_sales']:.1f}x")
    if multiples.get("multiple_note"):
        val_bits.append(str(multiples["multiple_note"]))
    if not val_bits:
        val_bits.append("valuation/EV fields incomplete — insufficient data honesty")
    parts.append("Valuation/EV: " + "; ".join(val_bits))

    # Bear / risk
    if bear_lines:
        # de-dupe preserve order
        seen: set[str] = set()
        uniq: list[str] = []
        for b in bear_lines:
            if b not in seen:
                seen.add(b)
                uniq.append(b)
        parts.append("Bear/risk: " + "; ".join(uniq[:4]))
    elif coverage < 3:
        parts.append("Bear/risk: sparse coverage — treat as insufficient data")
    else:
        parts.append("Bear/risk: no major soft flags from available fields")

    return " | ".join(parts)


def _score_fundamentals(metrics: dict[str, Any]) -> dict[str, Any]:
    """Map available metrics -> direction / confidence / quality / valuation notes."""
    if not metrics:
        capital = rebuild_capital_structure({})
        multiples = sales_multiples({}, capital)
        dilution = dilution_soft_flags({})
        reasoning = _format_desk_reasoning(
            quality_bits=[],
            valuation_note="insufficient data",
            capital=capital,
            multiples=multiples,
            bear_lines=[],
            insufficient=True,
            coverage=0,
        )
        return {
            "direction": Direction.NEUTRAL,
            "confidence": 15.0,
            "quality_score": 50.0,
            "valuation_note": "insufficient data",
            "raw_score": 0.0,
            "reasons": [reasoning],
            "metrics_used": {},
            "coverage": 0,
            "capital": capital,
            "multiples": multiples,
            "dilution": dilution,
            "bear_lines": [],
            "desk_reasoning": reasoning,
        }

    score = 0.0
    reasons: list[str] = []
    quality_bits: list[str] = []
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
            bit = f"Strong revenue growth ({rev_g:.1%})"
        elif rev_g >= 0.05:
            score += 10
            quality_pts += 70
            bit = f"Solid revenue growth ({rev_g:.1%})"
        elif rev_g >= 0:
            score += 3
            quality_pts += 55
            bit = f"Flat/low revenue growth ({rev_g:.1%})"
        elif rev_g >= -0.10:
            score -= 8
            quality_pts += 35
            bit = f"Mild revenue decline ({rev_g:.1%})"
        else:
            score -= 15
            quality_pts += 15
            bit = f"Sharp revenue decline ({rev_g:.1%})"
        reasons.append(bit)
        quality_bits.append(bit)

    earn_g = take("earningsGrowth")
    if earn_g is None:
        earn_g = take("earningsQuarterlyGrowth")
    if earn_g is not None:
        quality_n += 1
        if earn_g >= 0.20:
            score += 15
            quality_pts += 90
            bit = f"Strong earnings growth ({earn_g:.1%})"
        elif earn_g >= 0.05:
            score += 8
            quality_pts += 70
            bit = f"Positive earnings growth ({earn_g:.1%})"
        elif earn_g >= 0:
            score += 2
            quality_pts += 55
            bit = f"Flat earnings growth ({earn_g:.1%})"
        else:
            score -= 12
            quality_pts += 25
            bit = f"Negative earnings growth ({earn_g:.1%})"
        reasons.append(bit)
        quality_bits.append(bit)

    # --- Margins ---
    profit_m = take("profitMargins")
    if profit_m is not None:
        quality_n += 1
        if profit_m >= 0.20:
            score += 12
            quality_pts += 90
            bit = f"High profit margin ({profit_m:.1%})"
        elif profit_m >= 0.08:
            score += 6
            quality_pts += 70
            bit = f"Healthy profit margin ({profit_m:.1%})"
        elif profit_m >= 0:
            score += 0
            quality_pts += 50
            bit = f"Thin profit margin ({profit_m:.1%})"
        else:
            score -= 12
            quality_pts += 20
            bit = f"Negative profit margin ({profit_m:.1%})"
        reasons.append(bit)
        quality_bits.append(bit)

    op_m = take("operatingMargins")
    if op_m is not None:
        quality_n += 1
        if op_m >= 0.15:
            score += 8
            quality_pts += 85
            bit = f"Strong operating margin ({op_m:.1%})"
        elif op_m >= 0.05:
            score += 4
            quality_pts += 65
            bit = f"OK operating margin ({op_m:.1%})"
        elif op_m >= 0:
            quality_pts += 45
            bit = f"Thin operating margin ({op_m:.1%})"
        else:
            score -= 10
            quality_pts += 15
            bit = f"Negative operating margin ({op_m:.1%})"
        reasons.append(bit)
        quality_bits.append(bit)

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
            quality_bits.append(note)
        else:
            score -= 12
            quality_pts += 20
            note = f"Negative FCF ({fcf:,.0f}) — cash burn risk"
            reasons.append(note)
            quality_bits.append("Cash burn (neg FCF)")

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
            bit = f"Low leverage (D/E {dte_x:.2f}x)"
        elif dte_x <= 1.5:
            score += 2
            quality_pts += 60
            bit = f"Moderate leverage (D/E {dte_x:.2f}x)"
        elif dte_x <= 3.0:
            score -= 6
            quality_pts += 35
            bit = f"Elevated leverage (D/E {dte_x:.2f}x)"
        else:
            score -= 12
            quality_pts += 15
            bit = f"High leverage (D/E {dte_x:.2f}x)"
        reasons.append(bit)
        quality_bits.append(bit)

    current = take("currentRatio")
    if current is not None:
        quality_n += 1
        if current >= 1.5:
            score += 6
            quality_pts += 80
            bit = f"Strong current ratio ({current:.2f})"
        elif current >= 1.0:
            score += 2
            quality_pts += 60
            bit = f"Adequate current ratio ({current:.2f})"
        else:
            score -= 8
            quality_pts += 25
            bit = f"Weak current ratio ({current:.2f})"
        reasons.append(bit)
        quality_bits.append(bit)

    roe = take("returnOnEquity")
    if roe is not None:
        quality_n += 1
        if roe >= 0.20:
            score += 8
            quality_pts += 90
            bit = f"High ROE ({roe:.1%})"
        elif roe >= 0.08:
            score += 4
            quality_pts += 70
            bit = f"Decent ROE ({roe:.1%})"
        elif roe >= 0:
            quality_pts += 45
            bit = f"Low ROE ({roe:.1%})"
        else:
            score -= 8
            quality_pts += 20
            bit = f"Negative ROE ({roe:.1%})"
        reasons.append(bit)
        quality_bits.append(bit)

    # --- Valuation (base bands; desk overlays add EV/sales theme notes) ---
    pe = take("trailingPE")
    if pe is None:
        pe = take("forwardPE")
        if pe is not None and pe < 0:
            score -= 8
            reasons.append(f"Negative forward P/E ({pe:.1f}) — unprofitable")
            pe = None

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

    # --- Desk overlays: capital structure, EV/sales, dilution (soft only) ---
    capital = rebuild_capital_structure(metrics)
    multiples = sales_multiples(metrics, capital)
    dilution = dilution_soft_flags(metrics)

    # Record capital fields into metrics_used when present
    for k in ("market_cap", "cash", "debt", "ev", "shares_out"):
        if capital.get(k) is not None:
            metrics_used[k] = capital[k]
    if multiples.get("ev_to_sales") is not None:
        metrics_used["ev_to_sales"] = multiples["ev_to_sales"]
    if multiples.get("price_to_sales") is not None and "priceToSalesTrailing12Months" not in metrics_used:
        metrics_used["price_to_sales"] = multiples["price_to_sales"]

    score, reasons, bear_lines = _apply_desk_overlays(
        score,
        reasons,
        capital=capital,
        multiples=multiples,
        dilution=dilution,
        metrics=metrics,
    )

    # Leverage bear line when elevated
    dte_x = metrics_used.get("debt_to_equity_x")
    if isinstance(dte_x, (int, float)) and dte_x > 1.5:
        bear_lines.append(f"Leverage D/E {dte_x:.2f}x")

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

    conf = 25.0 + abs(score) * 0.9 + min(coverage, 10) * 2.5
    if coverage < 3:
        conf = min(conf, 35.0)
    confidence = float(max(5.0, min(92.0, conf)))

    desk_reasoning = _format_desk_reasoning(
        quality_bits=quality_bits,
        valuation_note=valuation_note,
        capital=capital,
        multiples=multiples,
        bear_lines=bear_lines,
        insufficient=False,
        coverage=coverage,
    )

    return {
        "direction": direction,
        "confidence": confidence,
        "quality_score": round(quality_score, 1),
        "valuation_note": valuation_note,
        "raw_score": round(score, 2),
        "reasons": reasons,
        "metrics_used": metrics_used,
        "coverage": coverage,
        "capital": capital,
        "multiples": multiples,
        "dilution": dilution,
        "bear_lines": bear_lines,
        "desk_reasoning": desk_reasoning,
    }


class FundamentalAgent(ResearchAgent):
    """Quality + valuation signals from free yfinance fundamentals (desk-style v2).

    Adds capital-structure / EV rebuild, sales multiples, and dilution soft flags.
    Soft score overlays only — never hard-blocks. Missing fields → honest
    insufficient-data reasoning; never fabricates filings/unlocks/insiders.
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

        snap: dict[str, Any] | None = None
        try:
            snap = self.market_data.snapshot(ticker)
        except Exception as exc:  # noqa: BLE001
            snap = {"error": str(exc)}

        capital = result.get("capital") or {}
        multiples = result.get("multiples") or {}
        dilution = result.get("dilution") or {}

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
            # Capital structure / EV (v2)
            "market_cap": capital.get("market_cap"),
            "cash": capital.get("cash"),
            "debt": capital.get("debt"),
            "ev": capital.get("ev"),
            "shares_out": capital.get("shares_out"),
            "ev_source": capital.get("ev_source"),
            "net_cash": capital.get("net_cash"),
            # Sales multiples
            "revenue": multiples.get("revenue"),
            "revenue_growth": multiples.get("revenue_growth"),
            "price_to_sales": multiples.get("price_to_sales"),
            "ev_to_sales": multiples.get("ev_to_sales"),
            "multiple_extreme": multiples.get("multiple_extreme"),
            "multiple_note": multiples.get("multiple_note"),
            # Dilution soft flags
            "dilution_flag": dilution.get("dilution_flag"),
            "dilution_reasons": dilution.get("dilution_reasons"),
            "float_shares": dilution.get("float_shares"),
            "implied_shares": dilution.get("implied_shares"),
            "share_count_gap_pct": dilution.get("share_count_gap_pct"),
            "bear_lines": result.get("bear_lines") or [],
        }
        if capital.get("ev_estimate") is not None:
            supporting["ev_estimate"] = capital["ev_estimate"]
        if capital.get("short_term_investments") is not None:
            supporting["short_term_investments"] = capital["short_term_investments"]

        return Signal(
            agent_id=self.agent_id,
            ticker=ticker,
            direction=result["direction"],
            confidence=float(result["confidence"]),
            reasoning=result.get("desk_reasoning") or "; ".join(result["reasons"]),
            supporting_data=supporting,
        )
