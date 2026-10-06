"""Lab bar data: Alpaca daily-bar provider, shared CSV cache, and the data ceiling.

The cache lives under ``lab_data_dir()/bars`` (one per machine, shared by every
bot checkout). Nothing past the machine's data ceiling is ever downloaded, and
anything past it that shows up in the cache is dropped on load.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from quant_system.backtest.cache import load_bars, save_bars
from quant_system.lab.settings import LabSettings, data_ceiling, lab_data_dir, load_lab_settings

MARKET_TZ = ZoneInfo("America/New_York")
MANIFEST_NAME = "_manifest.json"
_ALPACA_COLUMNS = {"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}


class BarsProvider(Protocol):
    source: str

    def fetch(self, symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        """Daily OHLCV per symbol, indexed by session date, start..end inclusive."""


def bars_dir(data_dir: Path | str | None = None) -> Path:
    """``<lab data dir>/bars``; ``data_dir`` overrides the lab data root."""
    return (Path(data_dir) if data_dir else lab_data_dir()) / "bars"


def _alpaca_frame_to_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Alpaca bar frame (UTC timestamps) → Open..Volume indexed by ET session date."""
    out = df.rename(columns=_ALPACA_COLUMNS)[list(_ALPACA_COLUMNS.values())]
    idx = pd.DatetimeIndex(out.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    out.index = idx.tz_convert(MARKET_TZ).tz_localize(None).normalize()
    return out


class AlpacaBarsProvider:
    """Daily bars from Alpaca's market-data API (works with paper keys)."""

    source = "alpaca"

    def __init__(
        self,
        client: Any | None = None,
        *,
        feed: str = "sip",
        adjustment: str = "all",
        batch_size: int = 100,
    ):
        self._client = client
        self.feed = feed
        self.adjustment = adjustment
        self.batch_size = int(batch_size)

    def _get_client(self) -> Any:
        if self._client is None:
            from alpaca.data.historical import StockHistoricalDataClient

            from quant_system.config.loader import alpaca_credentials

            creds = alpaca_credentials()
            if not creds["configured"]:
                raise RuntimeError(
                    "Alpaca keys not set (ALPACA_API_KEY / ALPACA_SECRET_KEY); "
                    "paper keys work for market data"
                )
            self._client = StockHistoricalDataClient(creds["api_key"], creds["secret_key"])
        return self._client

    def fetch(self, symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        client = self._get_client()
        out: dict[str, pd.DataFrame] = {}
        for i in range(0, len(symbols), self.batch_size):
            batch = symbols[i : i + self.batch_size]
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=datetime.combine(start, time.min, MARKET_TZ),
                end=datetime.combine(end, time.max, MARKET_TZ),
                adjustment=Adjustment(self.adjustment),
                feed=DataFeed(self.feed),
            )
            df = client.get_stock_bars(req).df
            if df is None or df.empty:
                continue
            for sym, frame in df.groupby(level="symbol"):
                out[str(sym).upper()] = _alpaca_frame_to_ohlcv(frame.droplevel("symbol"))
        return out


def _clip(df: pd.DataFrame, ceiling: date) -> pd.DataFrame:
    return df.loc[df.index <= pd.Timestamp(ceiling)]


def read_manifest(data_dir: Path | str | None = None) -> dict[str, Any]:
    path = bars_dir(data_dir) / MANIFEST_NAME
    if not path.is_file():
        return {"tickers": {}, "missing": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_manifest(manifest: dict[str, Any], data_dir: Path | str | None) -> None:
    path = bars_dir(data_dir) / MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sync_bars(
    symbols: Iterable[str],
    *,
    provider: BarsProvider | None = None,
    settings: LabSettings | None = None,
    data_dir: Path | str | None = None,
    force: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    """Download full adjusted history (start..ceiling) for symbols not yet current.

    Whole history is refetched rather than appended, because adjusted prices
    change retroactively after every split or dividend.
    """
    s = settings or load_lab_settings()
    ceiling = data_ceiling(s, today=today)
    start = s.data.start
    prov = provider or AlpacaBarsProvider(feed=s.data.feed, adjustment=s.data.adjustment)
    wanted = list(dict.fromkeys(str(x).strip().upper() for x in symbols if str(x).strip()))
    bdir = bars_dir(data_dir)
    manifest = read_manifest(data_dir)
    known: dict[str, Any] = manifest.setdefault("tickers", {})
    missing: dict[str, Any] = manifest.setdefault("missing", {})

    def _current(sym: str) -> bool:
        entry = known.get(sym) or missing.get(sym)
        return bool(entry) and entry.get("fetched_through", "") >= ceiling.isoformat()

    todo = wanted if force else [t for t in wanted if not _current(t)]
    fetched = prov.fetch(todo, start, ceiling) if todo else {}
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for sym in todo:
        df = fetched.get(sym)
        if df is None or df.empty:
            known.pop(sym, None)
            missing[sym] = {"fetched_through": ceiling.isoformat(), "synced_at": stamp}
            continue
        df = _clip(df, ceiling)
        save_bars(sym, df, bdir)
        missing.pop(sym, None)
        known[sym] = {
            "first": str(df.index.min().date()),
            "last": str(df.index.max().date()),
            "rows": int(len(df)),
            "fetched_through": ceiling.isoformat(),
            "synced_at": stamp,
        }
    manifest.update(
        {
            "source": prov.source,
            "feed": s.data.feed,
            "adjustment": s.data.adjustment,
            "start": start.isoformat(),
            "ceiling": ceiling.isoformat(),
        }
    )
    _write_manifest(manifest, data_dir)
    return {
        "ceiling": ceiling.isoformat(),
        "requested": len(wanted),
        "fetched": sorted(t for t in todo if t in known),
        "missing": sorted(t for t in todo if t in missing),
        "already_current": len(wanted) - len(todo),
        "cache_dir": str(bdir),
    }


def load_lab_bars(
    symbols: Iterable[str],
    *,
    settings: LabSettings | None = None,
    data_dir: Path | str | None = None,
    today: date | None = None,
) -> dict[str, pd.DataFrame]:
    """Cached bars for symbols, with every row past the data ceiling dropped."""
    ceiling = data_ceiling(settings or load_lab_settings(), today=today)
    out: dict[str, pd.DataFrame] = {}
    for raw in symbols:
        sym = str(raw).strip().upper()
        df = load_bars(sym, bars_dir(data_dir))
        if df is None:
            continue
        df = _clip(df, ceiling)
        if not df.empty:
            out[sym] = df
    return out


def bars_panel(bars: dict[str, pd.DataFrame], field: str) -> pd.DataFrame:
    """One OHLCV field as a dates x symbols frame (union of sessions, NaN where no bar)."""
    if not bars:
        return pd.DataFrame()
    return pd.DataFrame({sym: df[field] for sym, df in bars.items()}).sort_index()


def cache_status(
    *,
    settings: LabSettings | None = None,
    data_dir: Path | str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Summary of the cache, flagging any file holding rows past the ceiling."""
    ceiling = data_ceiling(settings or load_lab_settings(), today=today)
    manifest = read_manifest(data_dir)
    tickers = manifest.get("tickers") or {}
    over = sorted(t for t, e in tickers.items() if e.get("last", "") > ceiling.isoformat())
    return {
        "cache_dir": str(bars_dir(data_dir)),
        "ceiling": ceiling.isoformat(),
        "source": manifest.get("source"),
        "feed": manifest.get("feed"),
        "tickers": len(tickers),
        "missing": sorted(manifest.get("missing") or {}),
        "rows_past_ceiling": over,
        "stale": sorted(
            t for t, e in tickers.items() if e.get("fetched_through", "") < ceiling.isoformat()
        ),
    }
