"""FundamentalAgent — quality + valuation desk overlay from free yfinance fundamentals."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
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


# Statement row aliases (yfinance labels vary by ticker / period).
_REVENUE_ALIASES: tuple[str, ...] = ("Total Revenue", "Operating Revenue", "Revenue")
_NET_INCOME_ALIASES: tuple[str, ...] = (
    "Net Income",
    "Net Income Common Stockholders",
    "Net Income Continuous Operations",
    "Net Income From Continuing Operation Net Minority Interest",
)
_OPERATING_INCOME_ALIASES: tuple[str, ...] = ("Operating Income", "EBIT")
_GROSS_PROFIT_ALIASES: tuple[str, ...] = ("Gross Profit",)
_FCF_ALIASES: tuple[str, ...] = ("Free Cash Flow",)
_OCF_ALIASES: tuple[str, ...] = ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities")
_TOTAL_DEBT_ALIASES: tuple[str, ...] = ("Total Debt",)
_NET_DEBT_ALIASES: tuple[str, ...] = ("Net Debt",)
_CASH_ALIASES: tuple[str, ...] = (
    "Cash And Cash Equivalents",
    "Cash Cash Equivalents And Short Term Investments",
    "Cash Financial",
)
_CASH_STI_ALIASES: tuple[str, ...] = ("Cash Cash Equivalents And Short Term Investments",)
_SHARES_ALIASES: tuple[str, ...] = ("Ordinary Shares Number", "Share Issued", "Common Stock")
_EQUITY_ALIASES: tuple[str, ...] = (
    "Stockholders Equity",
    "Common Stock Equity",
    "Total Equity Gross Minority Interest",
)
_ASSETS_ALIASES: tuple[str, ...] = ("Total Assets",)
_META_KEYS: frozenset[str] = frozenset(
    {
        "fundamentals_source",
        "longBusinessSummary",
        "description",
        "_info_text",
    }
)

_MEANINGFUL_SKIP: frozenset[str] = frozenset(
    {
        "marketCap",
        "currentPrice",
        "regularMarketPrice",
        "previousClose",
        "longBusinessSummary",
        "description",
        "_info_text",
        "fundamentals_source",
    }
)


def _frame_row(frame: Any, aliases: tuple[str, ...]) -> Any | None:
    """Return a pandas Series for the first matching row label, or None."""
    if frame is None:
        return None
    try:
        if getattr(frame, "empty", True):
            return None
        index = frame.index
        for alias in aliases:
            if alias in index:
                return frame.loc[alias]
    except Exception:  # noqa: BLE001
        return None
    return None


def _series_value(series: Any, col_idx: int = 0) -> float | None:
    if series is None:
        return None
    try:
        if hasattr(series, "iloc"):
            if len(series) <= col_idx:
                return None
            return _safe_float(series.iloc[col_idx])
        return _safe_float(series)
    except Exception:  # noqa: BLE001
        return None


def _latest(frame: Any, aliases: tuple[str, ...]) -> float | None:
    return _series_value(_frame_row(frame, aliases), 0)


def _yoy_growth(frame: Any, aliases: tuple[str, ...]) -> float | None:
    series = _frame_row(frame, aliases)
    if series is None:
        return None
    try:
        if not hasattr(series, "iloc") or len(series) < 2:
            return None
        curr = _safe_float(series.iloc[0])
        prev = _safe_float(series.iloc[1])
    except Exception:  # noqa: BLE001
        return None
    if curr is None or prev is None or prev == 0:
        return None
    return (curr - prev) / abs(prev)


def _fast_info_dict(fast_info: Any) -> dict[str, Any]:
    """Normalize yfinance fast_info (object or mapping) to a plain dict."""
    if fast_info is None:
        return {}
    out: dict[str, Any] = {}
    if isinstance(fast_info, dict):
        src = fast_info
    else:
        try:
            src = dict(fast_info)
        except Exception:  # noqa: BLE001
            src = {}
            for key in (
                "market_cap",
                "marketCap",
                "last_price",
                "lastPrice",
                "previous_close",
                "previousClose",
                "shares",
                "shares_outstanding",
            ):
                try:
                    val = getattr(fast_info, key, None)
                except Exception:  # noqa: BLE001
                    val = None
                if val is not None:
                    src[key] = val
    for k, v in src.items():
        out[str(k)] = v
    return out


def metrics_from_statements(
    income_stmt: Any = None,
    balance_sheet: Any = None,
    cashflow: Any = None,
    *,
    fast_info: Any = None,
    quarterly_income: Any = None,
    quarterly_balance: Any = None,
    quarterly_cashflow: Any = None,
) -> dict[str, Any]:
    """Derive yfinance-style fundamental metrics from statement frames + fast_info.

    Prefers annual statements; falls back to quarterly frames when annual rows
    are missing. Pure helper — no network. Returns {} when nothing usable.
    """
    out: dict[str, Any] = {}

    # Prefer annual; use quarterly only as fill for missing keys.
    income = income_stmt if income_stmt is not None and not getattr(income_stmt, "empty", True) else None
    balance = balance_sheet if balance_sheet is not None and not getattr(balance_sheet, "empty", True) else None
    cash = cashflow if cashflow is not None and not getattr(cashflow, "empty", True) else None
    q_income = (
        quarterly_income
        if quarterly_income is not None and not getattr(quarterly_income, "empty", True)
        else None
    )
    q_balance = (
        quarterly_balance
        if quarterly_balance is not None and not getattr(quarterly_balance, "empty", True)
        else None
    )
    q_cash = (
        quarterly_cashflow
        if quarterly_cashflow is not None and not getattr(quarterly_cashflow, "empty", True)
        else None
    )

    def pick(annual_frame: Any, quarterly_frame: Any, aliases: tuple[str, ...]) -> float | None:
        val = _latest(annual_frame, aliases)
        if val is not None:
            return val
        return _latest(quarterly_frame, aliases)

    total_revenue = pick(income, q_income, _REVENUE_ALIASES)
    if total_revenue is not None:
        out["totalRevenue"] = total_revenue

    net_income = pick(income, q_income, _NET_INCOME_ALIASES)
    operating_income = pick(income, q_income, _OPERATING_INCOME_ALIASES)
    gross_profit = pick(income, q_income, _GROSS_PROFIT_ALIASES)

    fcf = pick(cash, q_cash, _FCF_ALIASES)
    if fcf is not None:
        out["freeCashflow"] = fcf
    ocf = pick(cash, q_cash, _OCF_ALIASES)
    if ocf is not None:
        out["operatingCashflow"] = ocf

    total_cash = pick(balance, q_balance, _CASH_ALIASES)
    if total_cash is not None:
        out["totalCash"] = total_cash
    cash_sti = pick(balance, q_balance, _CASH_STI_ALIASES)
    # Prefer broader cash+STI when distinct and larger; keep shortTermInvestments absense honest.
    if cash_sti is not None and (total_cash is None or cash_sti > total_cash):
        out["totalCash"] = cash_sti

    total_debt = pick(balance, q_balance, _TOTAL_DEBT_ALIASES)
    if total_debt is None:
        net_debt = pick(balance, q_balance, _NET_DEBT_ALIASES)
        cash_for_debt = out.get("totalCash")
        if net_debt is not None and cash_for_debt is not None:
            # Net Debt ≈ Total Debt - cash → Total Debt ≈ Net Debt + cash
            total_debt = net_debt + cash_for_debt
    if total_debt is not None:
        out["totalDebt"] = total_debt

    shares = pick(balance, q_balance, _SHARES_ALIASES)
    if shares is not None and shares > 0:
        out["sharesOutstanding"] = shares

    equity = pick(balance, q_balance, _EQUITY_ALIASES)
    assets = pick(balance, q_balance, _ASSETS_ALIASES)

    # Margins from latest annual/quarterly income vs revenue
    rev_for_margins = total_revenue
    if rev_for_margins is not None and rev_for_margins != 0:
        if net_income is not None:
            out["profitMargins"] = net_income / rev_for_margins
        if operating_income is not None:
            out["operatingMargins"] = operating_income / rev_for_margins
        if gross_profit is not None:
            out["grossMargins"] = gross_profit / rev_for_margins

    # YoY growth — prefer annual columns
    rev_g = _yoy_growth(income, _REVENUE_ALIASES)
    if rev_g is None:
        rev_g = _yoy_growth(q_income, _REVENUE_ALIASES)
    if rev_g is not None:
        out["revenueGrowth"] = rev_g

    earn_g = _yoy_growth(income, _NET_INCOME_ALIASES)
    if earn_g is None:
        earn_g = _yoy_growth(q_income, _NET_INCOME_ALIASES)
    if earn_g is not None:
        out["earningsGrowth"] = earn_g

    if equity is not None and equity != 0 and net_income is not None:
        out["returnOnEquity"] = net_income / equity
    if assets is not None and assets != 0 and net_income is not None:
        out["returnOnAssets"] = net_income / assets

    if total_debt is not None and equity is not None and equity != 0:
        # Match yfinance percent-like D/E (e.g. 25.0 ≈ 0.25x)
        out["debtToEquity"] = (total_debt / equity) * 100.0

    # fast_info market / price / shares fill
    fi = _fast_info_dict(fast_info)
    mcap = _safe_float(fi.get("marketCap") if "marketCap" in fi else fi.get("market_cap"))
    if mcap is not None:
        out["marketCap"] = mcap
    last_px = _safe_float(fi.get("lastPrice") if "lastPrice" in fi else fi.get("last_price"))
    if last_px is not None:
        out["currentPrice"] = last_px
        out["regularMarketPrice"] = last_px
    prev_px = _safe_float(
        fi.get("previousClose") if "previousClose" in fi else fi.get("previous_close")
    )
    if prev_px is not None:
        out["previousClose"] = prev_px
    fi_shares = _safe_float(fi.get("shares") if "shares" in fi else fi.get("shares_outstanding"))
    if fi_shares is not None and fi_shares > 0 and "sharesOutstanding" not in out:
        out["sharesOutstanding"] = fi_shares

    # Derived valuation
    debt = _safe_float(out.get("totalDebt"))
    cash_v = _safe_float(out.get("totalCash"))
    if mcap is not None and (debt is not None or cash_v is not None):
        out["enterpriseValue"] = mcap + (debt or 0.0) - (cash_v or 0.0)

    rev = _safe_float(out.get("totalRevenue"))
    if mcap is not None and rev is not None and rev > 0:
        out["priceToSalesTrailing12Months"] = mcap / rev
    ev = _safe_float(out.get("enterpriseValue"))
    if ev is not None and rev is not None and rev > 0:
        out["enterpriseToRevenue"] = ev / rev

    meaningful = {k for k in out if k not in _MEANINGFUL_SKIP}
    if not meaningful:
        return {}
    return out


def _info_has_meaningful(info: dict[str, Any]) -> bool:
    if not isinstance(info, dict) or not info:
        return False
    for key in _INFO_KEYS:
        if key in _MEANINGFUL_SKIP:
            continue
        if _safe_float(info.get(key)) is not None:
            return True
    return False


def _metrics_from_info(info: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _INFO_KEYS:
        val = _safe_float(info.get(key))
        if val is not None:
            out[key] = val
    text_bits: list[str] = []
    for key in ("longBusinessSummary", "description"):
        raw = info.get(key)
        if isinstance(raw, str) and raw.strip():
            text_bits.append(raw.strip())
            out[key] = raw.strip()[:4000]
    if text_bits:
        out["_info_text"] = " ".join(text_bits)[:8000]
    return out


def fetch_yfinance_fundamentals(ticker: str) -> dict[str, Any]:
    """Pull fundamentals via yfinance.

    Prefers `.info` when it has meaningful keys. When info is empty/thin (Yahoo
    crumb 401), falls back to `fast_info` + annual/quarterly statements.
    Returns {} only when nothing usable. Sets `fundamentals_source` to
    ``yfinance_info`` | ``yfinance_statements`` | ``mixed``.
    """
    try:
        import yfinance as yf
    except Exception:  # noqa: BLE001
        return {}

    try:
        t = yf.Ticker(ticker)
    except Exception:  # noqa: BLE001 — network / API flakiness
        return {}

    info: dict[str, Any] = {}
    try:
        raw_info = t.info
        if isinstance(raw_info, dict):
            info = raw_info
    except Exception:  # noqa: BLE001
        info = {}

    info_metrics = _metrics_from_info(info) if info else {}
    info_ok = _info_has_meaningful(info_metrics)

    statement_metrics: dict[str, Any] = {}
    need_statements = not info_ok
    # Also fill when info is thin on statement-derived fields we care about.
    if info_ok:
        # Still attach source; skip statement fetch when info is rich enough.
        needed = ("totalRevenue", "freeCashflow", "totalDebt", "totalCash", "profitMargins")
        if sum(1 for k in needed if k in info_metrics) < 2:
            need_statements = True

    if need_statements:
        try:
            fast = getattr(t, "fast_info", None)
        except Exception:  # noqa: BLE001
            fast = None
        try:
            income = t.income_stmt
        except Exception:  # noqa: BLE001
            income = None
        try:
            balance = t.balance_sheet
        except Exception:  # noqa: BLE001
            balance = None
        try:
            cashflow = t.cashflow
        except Exception:  # noqa: BLE001
            cashflow = None
        try:
            q_income = t.quarterly_income_stmt
        except Exception:  # noqa: BLE001
            q_income = None
        try:
            q_balance = t.quarterly_balance_sheet
        except Exception:  # noqa: BLE001
            q_balance = None
        try:
            q_cash = t.quarterly_cashflow
        except Exception:  # noqa: BLE001
            q_cash = None
        statement_metrics = metrics_from_statements(
            income,
            balance,
            cashflow,
            fast_info=fast,
            quarterly_income=q_income,
            quarterly_balance=q_balance,
            quarterly_cashflow=q_cash,
        )

    if info_ok and statement_metrics:
        # Merge: info wins on overlapping keys; statements fill gaps.
        out = dict(statement_metrics)
        out.update(info_metrics)
        out["fundamentals_source"] = "mixed"
    elif info_ok:
        out = dict(info_metrics)
        out["fundamentals_source"] = "yfinance_info"
    elif statement_metrics:
        out = dict(statement_metrics)
        out["fundamentals_source"] = "yfinance_statements"
    else:
        return {}

    meaningful = {k for k in out if k not in _MEANINGFUL_SKIP}
    if not meaningful:
        return {}
    return out



# fundamentals_source tags:
#   yfinance_info | yfinance_statements | mixed — Yahoo-only paths
#   fmp                                          — FMP-only (Yahoo empty/failed)
#   yahoo_fmp                                    — Yahoo primary + FMP fill gaps
_FMP_BASE = "https://financialmodelingprep.com/api/v3"
_CORE_FUNDAMENTAL_KEYS: tuple[str, ...] = (
    "totalRevenue",
    "freeCashflow",
    "totalDebt",
    "totalCash",
    "profitMargins",
    "operatingMargins",
    "returnOnEquity",
    "debtToEquity",
    "trailingPE",
    "forwardPE",
    "priceToSalesTrailing12Months",
    "pegRatio",
    "returnOnAssets",
    "currentRatio",
    "enterpriseValue",
)


def _fmp_http_get_json(url: str, *, timeout: float = 12.0) -> Any:
    """GET JSON from FMP; return None on any failure (offline-tolerant)."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "quant-trading-system/paper"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, TypeError):
        return None
    except Exception:  # noqa: BLE001
        return None


def _fmp_first_obj(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list) and payload:
        first = payload[0]
        return first if isinstance(first, dict) else {}
    if isinstance(payload, dict):
        if any(str(k).lower().startswith("error") for k in payload):
            return {}
        return payload
    return {}


def _fmp_pct_or_ratio_to_decimal(value: float | None) -> float | None:
    """Normalize ROE/ROA/margins: FMP sometimes returns 15.5 (percent), Yahoo uses 0.155."""
    if value is None:
        return None
    if abs(value) > 1.0:
        return value / 100.0
    return value


def _fmp_de_to_yfinance(value: float | None) -> float | None:
    """Normalize D/E toward yfinance percent-like (e.g. 25.0 ≈ 0.25x)."""
    if value is None:
        return None
    if abs(value) <= 10.0:
        return value * 100.0
    return value


def _map_fmp_to_yfinance_keys(
    *,
    profile: dict[str, Any],
    key_metrics: dict[str, Any],
    ratios: dict[str, Any],
    income: dict[str, Any] | None = None,
    balance: dict[str, Any] | None = None,
    cashflow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map FMP profile/metrics/ratios(+optional statements) → yfinance-style keys."""
    out: dict[str, Any] = {}
    income = income or {}
    balance = balance or {}
    cashflow = cashflow or {}

    def put(key: str, *candidates: Any, transform=None) -> None:
        for cand in candidates:
            val = _safe_float(cand)
            if val is None:
                continue
            if transform is not None:
                val = transform(val)
                if val is None:
                    continue
            out[key] = val
            return

    put("marketCap", profile.get("mktCap"), profile.get("marketCap"))
    put("currentPrice", profile.get("price"))
    put("regularMarketPrice", profile.get("price"))
    desc = profile.get("description")
    if isinstance(desc, str) and desc.strip():
        out["description"] = desc.strip()[:4000]
        out["longBusinessSummary"] = desc.strip()[:4000]
        out["_info_text"] = desc.strip()[:8000]

    put("enterpriseValue", key_metrics.get("enterpriseValueTTM"), key_metrics.get("enterpriseValue"))
    put("trailingPE", key_metrics.get("peRatioTTM"), ratios.get("peRatioTTM"), ratios.get("priceEarningsRatioTTM"))
    put(
        "priceToSalesTrailing12Months",
        key_metrics.get("priceToSalesRatioTTM"),
        ratios.get("priceToSalesRatioTTM"),
    )
    put("priceToBook", key_metrics.get("pbRatioTTM"), key_metrics.get("ptbRatioTTM"), ratios.get("priceToBookRatioTTM"))
    put("enterpriseToRevenue", key_metrics.get("evToSalesTTM"))
    put("enterpriseToEbitda", key_metrics.get("enterpriseValueOverEBITDATTM"))
    put("pegRatio", key_metrics.get("pegRatioTTM"), ratios.get("pegRatioTTM"))
    put("currentRatio", key_metrics.get("currentRatioTTM"), ratios.get("currentRatioTTM"))
    put(
        "debtToEquity",
        key_metrics.get("debtToEquityTTM"),
        ratios.get("debtEquityRatioTTM"),
        transform=_fmp_de_to_yfinance,
    )
    put(
        "returnOnEquity",
        key_metrics.get("roeTTM"),
        ratios.get("returnOnEquityTTM"),
        transform=_fmp_pct_or_ratio_to_decimal,
    )
    put(
        "returnOnAssets",
        key_metrics.get("returnOnTangibleAssetsTTM"),
        ratios.get("returnOnAssetsTTM"),
        transform=_fmp_pct_or_ratio_to_decimal,
    )
    put("profitMargins", ratios.get("netProfitMarginTTM"), transform=_fmp_pct_or_ratio_to_decimal)
    put("operatingMargins", ratios.get("operatingProfitMarginTTM"), transform=_fmp_pct_or_ratio_to_decimal)
    put("grossMargins", ratios.get("grossProfitMarginTTM"), transform=_fmp_pct_or_ratio_to_decimal)

    put("totalRevenue", income.get("revenue"), income.get("totalRevenue"))
    put("freeCashflow", cashflow.get("freeCashFlow"), cashflow.get("freeCashflow"))
    put(
        "operatingCashflow",
        cashflow.get("operatingCashFlow"),
        cashflow.get("netCashProvidedByOperatingActivities"),
    )
    put(
        "totalCash",
        balance.get("cashAndCashEquivalents"),
        balance.get("cashAndShortTermInvestments"),
    )
    put("totalDebt", balance.get("totalDebt"))
    put(
        "sharesOutstanding",
        income.get("weightedAverageShsOut"),
        income.get("weightedAverageShsOutDil"),
        balance.get("commonStockSharesOutstanding"),
    )
    sti = _safe_float(balance.get("shortTermInvestments"))
    if sti is not None:
        out["shortTermInvestments"] = sti

    mcap = _safe_float(out.get("marketCap"))
    debt = _safe_float(out.get("totalDebt"))
    cash = _safe_float(out.get("totalCash"))
    if "enterpriseValue" not in out and mcap is not None and (debt is not None or cash is not None):
        out["enterpriseValue"] = mcap + (debt or 0.0) - (cash or 0.0)

    rev = _safe_float(out.get("totalRevenue"))
    if "priceToSalesTrailing12Months" not in out and mcap is not None and rev is not None and rev > 0:
        out["priceToSalesTrailing12Months"] = mcap / rev
    ev = _safe_float(out.get("enterpriseValue"))
    if "enterpriseToRevenue" not in out and ev is not None and rev is not None and rev > 0:
        out["enterpriseToRevenue"] = ev / rev

    equity = _safe_float(balance.get("totalStockholdersEquity") or balance.get("totalEquity"))
    if "debtToEquity" not in out and debt is not None and equity is not None and equity != 0:
        out["debtToEquity"] = (debt / equity) * 100.0

    net_income = _safe_float(income.get("netIncome"))
    if "profitMargins" not in out and net_income is not None and rev is not None and rev != 0:
        out["profitMargins"] = net_income / rev
    op_inc = _safe_float(income.get("operatingIncome"))
    if "operatingMargins" not in out and op_inc is not None and rev is not None and rev != 0:
        out["operatingMargins"] = op_inc / rev
    if "returnOnEquity" not in out and net_income is not None and equity is not None and equity != 0:
        out["returnOnEquity"] = net_income / equity
    assets = _safe_float(balance.get("totalAssets"))
    if "returnOnAssets" not in out and net_income is not None and assets is not None and assets != 0:
        out["returnOnAssets"] = net_income / assets

    meaningful = {k for k in out if k not in _MEANINGFUL_SKIP}
    if not meaningful:
        return {}
    return out


def fetch_fmp_fundamentals(ticker: str, api_key: str | None = None) -> dict[str, Any]:
    """Pull fundamentals from Financial Modeling Prep (free-tier v3 endpoints).

    Reads ``FMP_API_KEY`` from the environment when ``api_key`` is not passed.
    Returns {} if no key or on any HTTP/parse failure — never raises into the pipeline.
    Maps into the same yfinance-style metric keys used by scoring.
    """
    key = (api_key if api_key is not None else os.environ.get("FMP_API_KEY") or "").strip()
    if not key:
        return {}

    symbol = (ticker or "").strip().upper()
    if not symbol or not re.fullmatch(r"[A-Z0-9.\-]{1,12}", symbol):
        return {}

    q = urllib.parse.urlencode({"apikey": key})
    q1 = urllib.parse.urlencode({"apikey": key, "limit": "1"})

    try:
        profile = _fmp_first_obj(_fmp_http_get_json(f"{_FMP_BASE}/profile/{symbol}?{q}"))
        key_metrics = _fmp_first_obj(_fmp_http_get_json(f"{_FMP_BASE}/key-metrics-ttm/{symbol}?{q}"))
        ratios = _fmp_first_obj(_fmp_http_get_json(f"{_FMP_BASE}/ratios-ttm/{symbol}?{q}"))
        income = _fmp_first_obj(_fmp_http_get_json(f"{_FMP_BASE}/income-statement/{symbol}?{q1}"))
        balance = _fmp_first_obj(
            _fmp_http_get_json(f"{_FMP_BASE}/balance-sheet-statement/{symbol}?{q1}")
        )
        cashflow = _fmp_first_obj(
            _fmp_http_get_json(f"{_FMP_BASE}/cash-flow-statement/{symbol}?{q1}")
        )
        out = _map_fmp_to_yfinance_keys(
            profile=profile,
            key_metrics=key_metrics,
            ratios=ratios,
            income=income,
            balance=balance,
            cashflow=cashflow,
        )
    except Exception:  # noqa: BLE001
        return {}

    if not out:
        return {}
    out["fundamentals_source"] = "fmp"
    return out


def _core_fundamental_coverage(metrics: dict[str, Any]) -> int:
    return sum(1 for k in _CORE_FUNDAMENTAL_KEYS if _safe_float(metrics.get(k)) is not None)


def _is_rich_fundamentals(metrics: dict[str, Any]) -> bool:
    """Yahoo rich enough that FMP fallback is unnecessary."""
    if not _info_has_meaningful(metrics):
        return False
    return _core_fundamental_coverage(metrics) >= 4


def fetch_fundamentals_with_fallback(ticker: str) -> dict[str, Any]:
    """Yahoo/yfinance first; FMP fills gaps or replaces when Yahoo is empty/thin.

    - Yahoo rich → return Yahoo unchanged (FMP not called)
    - Yahoo empty + FMP data → ``fundamentals_source=fmp``
    - Yahoo thin + FMP data → merge (Yahoo wins on overlap), tag ``yahoo_fmp``
    - Both empty → {}
    """
    try:
        yahoo = fetch_yfinance_fundamentals(ticker) or {}
    except Exception:  # noqa: BLE001
        yahoo = {}

    if _is_rich_fundamentals(yahoo):
        return yahoo

    try:
        fmp = fetch_fmp_fundamentals(ticker) or {}
    except Exception:  # noqa: BLE001
        fmp = {}

    fmp_metrics = {k: v for k, v in fmp.items() if k != "fundamentals_source"}
    yahoo_metrics = {k: v for k, v in yahoo.items() if k != "fundamentals_source"}

    if not fmp_metrics:
        return yahoo if yahoo else {}

    if not yahoo_metrics or not _info_has_meaningful(yahoo_metrics):
        out = dict(fmp_metrics)
        out["fundamentals_source"] = "fmp"
        return out

    # Yahoo thin: FMP fills gaps; Yahoo wins on overlap
    out = dict(fmp_metrics)
    out.update(yahoo_metrics)
    out["fundamentals_source"] = "yahoo_fmp"
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
    """Quality + valuation signals from yfinance + optional FMP fallback (desk-style v2).

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
        self._fetch = fundamentals_fetcher or fetch_fundamentals_with_fallback

    def analyze(self, ticker: str, *, benchmark: str = "SPY") -> Signal:
        fetch_error: str | None = None
        fundamentals_source: str | None = None
        try:
            metrics = dict(self._fetch(ticker) or {})
        except Exception as exc:  # noqa: BLE001
            metrics = {}
            fetch_error = str(exc)

        raw_source = metrics.pop("fundamentals_source", None)
        if isinstance(raw_source, str) and raw_source.strip():
            fundamentals_source = raw_source.strip()

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
            "fundamentals_source": fundamentals_source,
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
