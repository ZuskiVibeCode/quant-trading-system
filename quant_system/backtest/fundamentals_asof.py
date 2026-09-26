"""As-of fundamentals for backtests — statement snapshots with no future peeking.

Strategy (documented approximation, not full SEC PIT):
1. Prefer FMP historical statements when the API returns them (uses ``filingDate`` /
   ``acceptedDate`` as the availability date).
2. Otherwise cache yfinance annual + quarterly statement frames (no ``.info`` —
   avoids Yahoo crumb 401 loops) and apply a conservative post-period filing lag
   before the columns become visible.
3. Market cap / price fields are rebuilt from as-of bar close × shares when possible
   so valuation multiples do not use live prices.

Cache layout: ``data/backtest/fundamentals/{TICKER}.json``
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pandas as pd

from quant_system.research.fundamental import (
    _map_fmp_to_yfinance_keys,
    _fmp_http_get_json,
    _FMP_BASE,
    metrics_from_statements,
)

# Conservative US filing lags when only period-end dates are known (yfinance).
# 10-Q often ~40 days after quarter-end; 10-K longer. We use the larger lag for
# annual columns and a slightly shorter lag for quarterly.
QUARTERLY_FILING_LAG_DAYS = 45
ANNUAL_FILING_LAG_DAYS = 60

APPROXIMATION_DOC = (
    "as-of statement snapshots: FMP filingDate/acceptedDate when available; "
    "else yfinance statement columns gated by period_end + "
    f"{QUARTERLY_FILING_LAG_DAYS}d (Q) / {ANNUAL_FILING_LAG_DAYS}d (A) lag; "
    "marketCap from as-of close × shares; not full SEC point-in-time"
)


def default_fundamentals_cache_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "backtest" / "fundamentals"


def _safe_ticker(ticker: str) -> str:
    return str(ticker).strip().upper().replace("^", "_").replace("/", "_")


def cache_path(ticker: str, cache_dir: Path | str | None = None) -> Path:
    cdir = Path(cache_dir) if cache_dir else default_fundamentals_cache_dir()
    return cdir / f"{_safe_ticker(ticker)}.json"


def _frame_to_records(df: Any) -> dict[str, Any] | None:
    """Serialize a yfinance-style statement frame (rows=line items, cols=dates)."""
    if df is None or getattr(df, "empty", True):
        return None
    try:
        out = df.copy()
        # Columns → ISO dates; index → line item labels
        cols = [str(pd.Timestamp(c).date()) for c in out.columns]
        out.columns = cols
        records: dict[str, dict[str, float | None]] = {}
        for label, row in out.iterrows():
            key = str(label)
            records[key] = {
                c: (None if pd.isna(v) else float(v)) for c, v in zip(cols, row.tolist())
            }
        return {"columns": cols, "rows": records}
    except Exception:  # noqa: BLE001
        return None


def _records_to_frame(payload: dict[str, Any] | None) -> pd.DataFrame | None:
    if not payload or not isinstance(payload, dict):
        return None
    cols = payload.get("columns") or []
    rows = payload.get("rows") or {}
    if not cols or not rows:
        return None
    try:
        data = {label: [row.get(c) for c in cols] for label, row in rows.items()}
        df = pd.DataFrame(data, index=cols).T
        df.columns = [pd.Timestamp(c) for c in df.columns]
        return df
    except Exception:  # noqa: BLE001
        return None


def _fetch_yfinance_statement_bundle(ticker: str) -> dict[str, Any]:
    """Pull statement frames only (no .info) — offline-tolerant."""
    try:
        import yfinance as yf
    except Exception:  # noqa: BLE001
        return {}
    try:
        t = yf.Ticker(ticker)
    except Exception:  # noqa: BLE001
        return {}

    bundle: dict[str, Any] = {"source": "yfinance_statements", "ticker": ticker.upper()}

    def grab(attr: str) -> Any:
        try:
            return getattr(t, attr)
        except Exception:  # noqa: BLE001
            return None

    mapping = {
        "income_stmt": "income_stmt",
        "balance_sheet": "balance_sheet",
        "cashflow": "cashflow",
        "quarterly_income_stmt": "quarterly_income_stmt",
        "quarterly_balance_sheet": "quarterly_balance_sheet",
        "quarterly_cashflow": "quarterly_cashflow",
    }
    frames: dict[str, Any] = {}
    for key, attr in mapping.items():
        rec = _frame_to_records(grab(attr))
        if rec:
            frames[key] = rec
    if not frames:
        return {}
    bundle["frames"] = frames
    # Optional fast_info shares (static-ish; price overridden as-of later)
    try:
        fi = getattr(t, "fast_info", None)
        if fi is not None:
            shares = None
            try:
                shares = float(fi.get("shares") if isinstance(fi, dict) else getattr(fi, "shares", None))
            except Exception:  # noqa: BLE001
                shares = None
            if shares and shares > 0:
                bundle["shares_hint"] = shares
    except Exception:  # noqa: BLE001
        pass
    return bundle


def _fetch_fmp_statement_history(ticker: str, *, limit: int = 40) -> dict[str, Any]:
    """Pull FMP income/balance/cashflow history with filing dates when API allows."""
    key = (os.environ.get("FMP_API_KEY") or "").strip()
    if not key:
        return {}
    symbol = ticker.strip().upper()
    if not symbol:
        return {}

    def rows(path: str) -> list[dict[str, Any]]:
        q = __import__("urllib.parse").parse.urlencode(
            {"symbol": symbol, "limit": str(limit), "apikey": key}
        )
        payload = _fmp_http_get_json(f"{_FMP_BASE}/{path}?{q}")
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        return []

    try:
        income = rows("income-statement")
        balance = rows("balance-sheet-statement")
        cashflow = rows("cash-flow-statement")
    except Exception:  # noqa: BLE001
        return {}
    if not income and not balance and not cashflow:
        return {}
    return {
        "source": "fmp_statements",
        "ticker": symbol,
        "income": income,
        "balance": balance,
        "cashflow": cashflow,
    }


def load_or_fetch_bundle(
    ticker: str,
    *,
    cache_dir: Path | str | None = None,
    force: bool = False,
    prefer_fmp: bool = True,
) -> dict[str, Any]:
    """Load cached fundamentals bundle or fetch + cache (FMP then yfinance)."""
    path = cache_path(ticker, cache_dir)
    if not force and path.is_file():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict) and (data.get("frames") or data.get("income")):
                return data
        except Exception:  # noqa: BLE001
            pass

    bundle: dict[str, Any] = {}
    if prefer_fmp:
        bundle = _fetch_fmp_statement_history(ticker) or {}
        # Small pause to be polite on multi-ticker warm
        if bundle:
            time.sleep(0.15)
    if not bundle:
        bundle = _fetch_yfinance_statement_bundle(ticker) or {}
    if not bundle:
        return {}

    cdir = Path(cache_dir) if cache_dir else default_fundamentals_cache_dir()
    cdir.mkdir(parents=True, exist_ok=True)
    bundle["cached_at"] = pd.Timestamp.now(tz="America/New_York").isoformat()
    path.write_text(json.dumps(bundle, indent=2, default=str) + "\n")
    return bundle


def ensure_fundamentals(
    tickers: list[str],
    *,
    cache_dir: Path | str | None = None,
    force: bool = False,
) -> dict[str, dict[str, Any]]:
    """Warm fundamentals cache for many tickers (skips network when cached)."""
    out: dict[str, dict[str, Any]] = {}
    for t in tickers:
        u = str(t).strip().upper()
        if not u:
            continue
        bundle = load_or_fetch_bundle(u, cache_dir=cache_dir, force=force)
        if bundle:
            out[u] = bundle
    return out


def _availability_date_fmp(row: dict[str, Any]) -> pd.Timestamp | None:
    for key in ("filingDate", "acceptedDate", "fillingDate"):
        raw = row.get(key)
        if not raw:
            continue
        try:
            # acceptedDate may include time
            ts = pd.Timestamp(str(raw)[:19] if "T" in str(raw) or " " in str(raw) else raw)
            if getattr(ts, "tz", None) is not None:
                ts = ts.tz_localize(None)
            return ts.normalize()
        except Exception:  # noqa: BLE001
            continue
    # Fallback: period end + annual/quarterly lag
    period = str(row.get("period") or "").upper()
    lag = ANNUAL_FILING_LAG_DAYS if period in ("FY", "ANNUAL") else QUARTERLY_FILING_LAG_DAYS
    raw = row.get("date")
    if not raw:
        return None
    try:
        ts = pd.Timestamp(raw).normalize()
        return ts + pd.Timedelta(days=lag)
    except Exception:  # noqa: BLE001
        return None


def _pick_fmp_asof(rows: list[dict[str, Any]], as_of: pd.Timestamp) -> dict[str, Any]:
    """Latest statement whose filing/availability date is <= as_of."""
    eligible: list[tuple[pd.Timestamp, dict[str, Any]]] = []
    for row in rows:
        avail = _availability_date_fmp(row)
        if avail is not None and avail <= as_of:
            eligible.append((avail, row))
    if not eligible:
        return {}
    eligible.sort(key=lambda x: x[0])
    return dict(eligible[-1][1])


def _truncate_yf_frame(
    df: pd.DataFrame | None,
    as_of: pd.Timestamp,
    *,
    lag_days: int,
) -> pd.DataFrame | None:
    if df is None or getattr(df, "empty", True):
        return None
    keep: list[Any] = []
    for col in df.columns:
        try:
            period_end = pd.Timestamp(col).normalize()
        except Exception:  # noqa: BLE001
            continue
        available = period_end + pd.Timedelta(days=int(lag_days))
        if available <= as_of:
            keep.append(col)
    if not keep:
        return None
    # Preserve original column order (typically newest-first)
    return df[keep]


def _metrics_from_fmp_asof(
    bundle: dict[str, Any],
    as_of: pd.Timestamp,
    *,
    asof_price: float | None,
) -> dict[str, Any]:
    income = _pick_fmp_asof(list(bundle.get("income") or []), as_of)
    balance = _pick_fmp_asof(list(bundle.get("balance") or []), as_of)
    cashflow = _pick_fmp_asof(list(bundle.get("cashflow") or []), as_of)
    if not income and not balance and not cashflow:
        return {}
    out = _map_fmp_to_yfinance_keys(
        profile={},
        key_metrics={},
        ratios={},
        income=income,
        balance=balance,
        cashflow=cashflow,
    )
    if not out:
        return {}
    # YoY growth if a prior statement exists
    income_rows = list(bundle.get("income") or [])
    prior = None
    if income:
        avail = _availability_date_fmp(income)
        older = []
        for row in income_rows:
            a = _availability_date_fmp(row)
            if a is not None and avail is not None and a < avail and a <= as_of:
                older.append((a, row))
        if older:
            older.sort(key=lambda x: x[0])
            prior = older[-1][1]
    if prior and income:
        for src_key, dest in (("revenue", "revenueGrowth"), ("netIncome", "earningsGrowth")):
            curr = income.get(src_key) if src_key != "revenue" else (income.get("revenue") or income.get("totalRevenue"))
            prev = prior.get(src_key) if src_key != "revenue" else (prior.get("revenue") or prior.get("totalRevenue"))
            try:
                curr_f = float(curr) if curr is not None else None
                prev_f = float(prev) if prev is not None else None
            except (TypeError, ValueError):
                curr_f = prev_f = None
            if curr_f is not None and prev_f is not None and prev_f != 0:
                out[dest] = (curr_f - prev_f) / abs(prev_f)

    shares = out.get("sharesOutstanding")
    if asof_price is not None and asof_price > 0 and shares and float(shares) > 0:
        out["currentPrice"] = float(asof_price)
        out["regularMarketPrice"] = float(asof_price)
        out["marketCap"] = float(asof_price) * float(shares)
        # Rebuild EV if debt/cash present
        debt = out.get("totalDebt")
        cash = out.get("totalCash")
        if debt is not None or cash is not None:
            out["enterpriseValue"] = out["marketCap"] + float(debt or 0) - float(cash or 0)
            rev = out.get("totalRevenue")
            if rev and float(rev) > 0:
                out["priceToSalesTrailing12Months"] = out["marketCap"] / float(rev)
                out["enterpriseToRevenue"] = out["enterpriseValue"] / float(rev)

    out["fundamentals_source"] = "fmp_asof"
    out["asof_approximation"] = APPROXIMATION_DOC
    out["asof_date"] = str(as_of.date())
    return out


def _metrics_from_yf_asof(
    bundle: dict[str, Any],
    as_of: pd.Timestamp,
    *,
    asof_price: float | None,
) -> dict[str, Any]:
    frames = bundle.get("frames") or {}
    income = _truncate_yf_frame(
        _records_to_frame(frames.get("income_stmt")), as_of, lag_days=ANNUAL_FILING_LAG_DAYS
    )
    balance = _truncate_yf_frame(
        _records_to_frame(frames.get("balance_sheet")), as_of, lag_days=ANNUAL_FILING_LAG_DAYS
    )
    cashflow = _truncate_yf_frame(
        _records_to_frame(frames.get("cashflow")), as_of, lag_days=ANNUAL_FILING_LAG_DAYS
    )
    q_income = _truncate_yf_frame(
        _records_to_frame(frames.get("quarterly_income_stmt")),
        as_of,
        lag_days=QUARTERLY_FILING_LAG_DAYS,
    )
    q_balance = _truncate_yf_frame(
        _records_to_frame(frames.get("quarterly_balance_sheet")),
        as_of,
        lag_days=QUARTERLY_FILING_LAG_DAYS,
    )
    q_cash = _truncate_yf_frame(
        _records_to_frame(frames.get("quarterly_cashflow")),
        as_of,
        lag_days=QUARTERLY_FILING_LAG_DAYS,
    )
    if all(x is None or getattr(x, "empty", True) for x in (income, balance, cashflow, q_income, q_balance, q_cash)):
        return {}

    fast_info = None
    shares_hint = bundle.get("shares_hint")
    if asof_price is not None or shares_hint:
        fast_info = {}
        if asof_price is not None:
            fast_info["lastPrice"] = float(asof_price)
        if shares_hint:
            fast_info["shares"] = float(shares_hint)

    out = metrics_from_statements(
        income,
        balance,
        cashflow,
        fast_info=fast_info,
        quarterly_income=q_income,
        quarterly_balance=q_balance,
        quarterly_cashflow=q_cash,
    )
    if not out:
        return {}
    # Prefer as-of price for mcap when shares known
    shares = out.get("sharesOutstanding") or shares_hint
    if asof_price is not None and asof_price > 0 and shares and float(shares) > 0:
        out["currentPrice"] = float(asof_price)
        out["regularMarketPrice"] = float(asof_price)
        out["sharesOutstanding"] = float(shares)
        out["marketCap"] = float(asof_price) * float(shares)
        debt = out.get("totalDebt")
        cash = out.get("totalCash")
        if debt is not None or cash is not None:
            out["enterpriseValue"] = out["marketCap"] + float(debt or 0) - float(cash or 0)
        rev = out.get("totalRevenue")
        if rev and float(rev) > 0:
            out["priceToSalesTrailing12Months"] = out["marketCap"] / float(rev)
            ev = out.get("enterpriseValue")
            if ev is not None:
                out["enterpriseToRevenue"] = float(ev) / float(rev)

    out["fundamentals_source"] = "yfinance_asof"
    out["asof_approximation"] = APPROXIMATION_DOC
    out["asof_date"] = str(as_of.date())
    return out


def metrics_asof(
    ticker: str,
    as_of: str | pd.Timestamp,
    *,
    cache_dir: Path | str | None = None,
    asof_price: float | None = None,
    bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return yfinance-style fundamental metrics visible on ``as_of`` (no lookahead)."""
    as_of_ts = pd.Timestamp(as_of).normalize()
    if getattr(as_of_ts, "tz", None) is not None:
        as_of_ts = as_of_ts.tz_localize(None)
    data = bundle if bundle is not None else load_or_fetch_bundle(ticker, cache_dir=cache_dir)
    if not data:
        return {}
    source = str(data.get("source") or "")
    if source.startswith("fmp") or data.get("income") is not None:
        return _metrics_from_fmp_asof(data, as_of_ts, asof_price=asof_price)
    return _metrics_from_yf_asof(data, as_of_ts, asof_price=asof_price)


def make_asof_fundamentals_fetcher(
    market_data: Any,
    *,
    cache_dir: Path | str | None = None,
    bundles: dict[str, dict[str, Any]] | None = None,
):
    """Callable for FundamentalAgent that respects AsOfMarketData.as_of."""

    def fetch(ticker: str) -> dict[str, Any]:
        as_of = getattr(market_data, "as_of", None)
        if as_of is None:
            # Safety: without as_of, refuse live peek — return empty (neutral)
            return {}
        price = None
        try:
            price = float(market_data.last_price(ticker))
        except Exception:  # noqa: BLE001
            price = None
        bundle = (bundles or {}).get(ticker.upper())
        return metrics_asof(
            ticker,
            as_of,
            cache_dir=cache_dir,
            asof_price=price,
            bundle=bundle,
        )

    return fetch
