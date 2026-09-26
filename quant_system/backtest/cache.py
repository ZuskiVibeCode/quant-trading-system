"""Download / load daily OHLCV bars for backtests (yfinance → CSV cache)."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd

# Macro proxies MacroAgent may request (as-of safe when cached).
MACRO_PROXY_TICKERS: tuple[str, ...] = (
    "SPY",
    "^VIX",
    "UUP",
    "TLT",
    "IEF",
    "^TNX",
)


def default_cache_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "backtest" / "bars"


def _safe_filename(ticker: str) -> str:
    """Map ticker symbols to filesystem-safe names (e.g. ^VIX → _VIX)."""
    return ticker.upper().replace("^", "_").replace("/", "_").replace("=", "_")


def bar_path(ticker: str, cache_dir: Path | str | None = None) -> Path:
    cdir = Path(cache_dir) if cache_dir else default_cache_dir()
    return cdir / f"{_safe_filename(ticker)}.csv"


def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    out = df.copy()
    if not isinstance(out.index, pd.DatetimeIndex):
        # First column may be Date if read from CSV without index
        if "Date" in out.columns:
            out["Date"] = pd.to_datetime(out["Date"], utc=False)
            out = out.set_index("Date")
        else:
            out.index = pd.to_datetime(out.index, utc=False)
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_localize(None)
    out.index = out.index.normalize()
    out = out.rename(columns=str.title)
    needed = ["Open", "High", "Low", "Close", "Volume"]
    for c in needed:
        if c not in out.columns:
            raise ValueError(f"Missing OHLCV column {c}")
    out = out[needed].apply(pd.to_numeric, errors="coerce").dropna(subset=["Close"])
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def load_bars(ticker: str, cache_dir: Path | str | None = None) -> pd.DataFrame | None:
    path = bar_path(ticker, cache_dir)
    if not path.is_file():
        return None
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    return _normalize_ohlcv(df)


def save_bars(ticker: str, df: pd.DataFrame, cache_dir: Path | str | None = None) -> Path:
    cdir = Path(cache_dir) if cache_dir else default_cache_dir()
    cdir.mkdir(parents=True, exist_ok=True)
    path = bar_path(ticker, cdir)
    norm = _normalize_ohlcv(df)
    norm.to_csv(path, index_label="Date")
    return path


def download_bars(
    tickers: Iterable[str],
    *,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    cache_dir: Path | str | None = None,
    force: bool = False,
) -> dict[str, pd.DataFrame]:
    """Fetch daily bars via yfinance; cache under data/backtest/bars/.

    Pads start by ~450 calendar days so agents requesting period=1y have history
    before the backtest window. End is inclusive (yfinance end is exclusive → +1d).
    """
    import yfinance as yf

    cdir = Path(cache_dir) if cache_dir else default_cache_dir()
    cdir.mkdir(parents=True, exist_ok=True)
    start_ts = pd.Timestamp(start).normalize()
    end_ts = pd.Timestamp(end).normalize()
    fetch_start = (start_ts - pd.Timedelta(days=450)).strftime("%Y-%m-%d")
    fetch_end = (end_ts + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    out: dict[str, pd.DataFrame] = {}
    for raw in tickers:
        ticker = str(raw).strip().upper()
        if not ticker:
            continue
        if not force:
            cached = load_bars(ticker, cdir)
            if cached is not None and not cached.empty:
                # Reuse if cache covers the padded window reasonably
                if cached.index.min() <= start_ts - pd.Timedelta(days=30) and cached.index.max() >= end_ts - pd.Timedelta(days=5):
                    out[ticker] = cached
                    continue
        try:
            raw_df = yf.download(
                ticker,
                start=fetch_start,
                end=fetch_end,
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=False,
            )
            # yfinance multi-index columns when single ticker sometimes
            if isinstance(raw_df.columns, pd.MultiIndex):
                raw_df.columns = [c[0] for c in raw_df.columns]
            if raw_df is None or raw_df.empty:
                # Fallback: Ticker.history
                raw_df = yf.Ticker(ticker).history(
                    start=fetch_start, end=fetch_end, interval="1d", auto_adjust=True
                )
            if raw_df is None or raw_df.empty:
                continue
            norm = _normalize_ohlcv(raw_df)
            save_bars(ticker, norm, cdir)
            out[ticker] = norm
        except Exception:  # noqa: BLE001 — skip bad tickers; harness tolerates missing
            continue
    return out


def ensure_bars(
    tickers: Iterable[str],
    *,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    cache_dir: Path | str | None = None,
    force: bool = False,
) -> dict[str, pd.DataFrame]:
    """Load from cache; download any missing (or all if force)."""
    cdir = Path(cache_dir) if cache_dir else default_cache_dir()
    wanted = [str(t).strip().upper() for t in tickers if str(t).strip()]
    loaded: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    start_ts = pd.Timestamp(start).normalize()
    end_ts = pd.Timestamp(end).normalize()
    for t in wanted:
        if force:
            missing.append(t)
            continue
        cached = load_bars(t, cdir)
        if cached is None or cached.empty:
            missing.append(t)
            continue
        if cached.index.max() < end_ts - pd.Timedelta(days=7):
            missing.append(t)
            continue
        if cached.index.min() > start_ts - pd.Timedelta(days=30):
            # Prefer richer history when agents need ~1y lookback
            missing.append(t)
            continue
        loaded[t] = cached
    if missing:
        fetched = download_bars(missing, start=start, end=end, cache_dir=cdir, force=True)
        loaded.update(fetched)
    return loaded
