"""Lab bar data: Alpaca daily-bar provider, shared CSV cache, and the data ceiling.

The cache lives under ``lab_data_dir()/bars`` (one per machine, shared by every
bot checkout). Nothing past the machine's data ceiling is ever downloaded, and
anything past it that shows up in the cache is dropped on load.

Warm-up: Alpaca has no bars before 2016, so ``lab_data_dir()/bars_warmup`` holds
yfinance bars from ``data.warmup.start`` to ``data.warmup.overlap_end``. On load,
each name's warm-up rows before ``data.start`` go in front of its Alpaca bars,
scaled by the median Alpaca / yfinance close ratio over the days both sources
have (``data.start`` .. ``overlap_end``). They feed indicators and the cost
model's lookback only: every window starts on or after ``data.start``, and from
there on Alpaca is the only source. A name whose sources disagree on the shared
days gets no warm-up and starts late.
"""

from __future__ import annotations

import json
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from quant_system.backtest.cache import load_bars, save_bars
from quant_system.lab.settings import LabSettings, data_ceiling, lab_data_dir, load_lab_settings

MARKET_TZ = ZoneInfo("America/New_York")
MANIFEST_NAME = "_manifest.json"
_ALPACA_COLUMNS = {"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}
# alpaca-py sets no network timeout (it only retries HTTP 429 / 504), so a stalled
# response would hang a sync. Every Alpaca call here gets one, and is retried.
REQUEST_TIMEOUT = (10.0, 60.0)  # seconds: connect, then between bytes of the response
REQUEST_RETRIES = 2
RETRY_WAIT = (5.0, 15.0)  # seconds before the first and second retry


def with_timeout(client: Any, timeout: tuple[float, float] = REQUEST_TIMEOUT) -> Any:
    """Give an alpaca-py client a default timeout on every HTTP request it sends."""
    session = getattr(client, "_session", None)
    if session is not None and not getattr(session, "_lab_timeout", None):
        send = session.request

        def request(method: str, url: str, **kwargs: Any) -> Any:
            kwargs.setdefault("timeout", timeout)
            return send(method, url, **kwargs)

        session.request = request
        session._lab_timeout = timeout
    return client


def with_retries(
    call: Callable[[], Any], what: str, *, retries: int = REQUEST_RETRIES,
    wait: Callable[[float], None] | None = None,
) -> Any:
    """``call()``, retried after a timeout or dropped connection; a RuntimeError once out of tries.

    Only network failures are retried. Alpaca's own refusals (``APIError``)
    pass straight through.
    """
    from requests.exceptions import RequestException

    sleep = wait or _time.sleep
    for attempt in range(retries + 1):
        try:
            return call()
        except RequestException as exc:
            if attempt == retries:
                raise RuntimeError(
                    f"{what}: no answer after {retries + 1} tries ({type(exc).__name__}: {exc}). "
                    "Stopping; rerun to resume."
                ) from None
            sleep(RETRY_WAIT[min(attempt, len(RETRY_WAIT) - 1)])


class BarsProvider(Protocol):
    source: str

    def fetch(self, symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        """Daily OHLCV per symbol, indexed by session date, start..end inclusive."""


def bars_dir(data_dir: Path | str | None = None) -> Path:
    """``<lab data dir>/bars``; ``data_dir`` overrides the lab data root."""
    return (Path(data_dir) if data_dir else lab_data_dir()) / "bars"


def warmup_dir(data_dir: Path | str | None = None) -> Path:
    """``<lab data dir>/bars_warmup``: yfinance bars before ``data.start``, for warm-up only."""
    return (Path(data_dir) if data_dir else lab_data_dir()) / "bars_warmup"


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
            self._client = with_timeout(StockHistoricalDataClient(creds["api_key"], creds["secret_key"]))
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
            df = with_retries(
                lambda: client.get_stock_bars(req).df, f"Alpaca daily bars {batch[0]}..{batch[-1]}"
            )
            if df is None or df.empty:
                continue
            for sym, frame in df.groupby(level="symbol"):
                out[str(sym).upper()] = _alpaca_frame_to_ohlcv(frame.droplevel("symbol"))
        return out


class YFinanceBarsProvider:
    """Daily bars from yfinance, split + dividend adjusted. Warm-up only (before Alpaca's history)."""

    source = "yfinance"

    def __init__(self, *, retries: int = 5, pause: float = 0.3):
        self.retries = int(retries)
        self.pause = float(pause)

    def fetch(self, symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        import time as _time

        import yfinance as yf
        from yfinance.exceptions import YFPricesMissingError, YFTickerMissingError, YFTzMissingError

        yf.config.debug.hide_exceptions = False
        out: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            df = None
            wait = 30.0
            for attempt in range(self.retries + 1):
                try:
                    df = yf.Ticker(sym.replace(".", "-")).history(
                        start=start.isoformat(),
                        end=(end + timedelta(days=1)).isoformat(),  # yfinance's end is exclusive
                        interval="1d",
                        auto_adjust=True,
                    )
                    break
                except (YFPricesMissingError, YFTickerMissingError, YFTzMissingError):
                    break  # Yahoo has nothing for this name in the range
                except Exception:  # noqa: BLE001 — rate limit / network: back off, retry
                    if attempt == self.retries:
                        raise
                    _time.sleep(wait)
                    wait *= 2
            if df is not None and not df.empty:
                df = df[list(_ALPACA_COLUMNS.values())].copy()
                idx = pd.DatetimeIndex(df.index)
                df.index = (idx.tz_localize(None) if idx.tz is not None else idx).normalize()
                out[sym] = df
            _time.sleep(self.pause)
        return out


def _clip(df: pd.DataFrame, ceiling: date) -> pd.DataFrame:
    return df.loc[df.index <= pd.Timestamp(ceiling)]


def _read_manifest_in(directory: Path) -> dict[str, Any]:
    path = directory / MANIFEST_NAME
    if not path.is_file():
        return {"tickers": {}, "missing": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_manifest_in(directory: Path, manifest: dict[str, Any]) -> None:
    path = directory / MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_manifest(data_dir: Path | str | None = None) -> dict[str, Any]:
    return _read_manifest_in(bars_dir(data_dir))


def read_warmup_manifest(data_dir: Path | str | None = None) -> dict[str, Any]:
    return _read_manifest_in(warmup_dir(data_dir))


def _write_manifest(manifest: dict[str, Any], data_dir: Path | str | None) -> None:
    _write_manifest_in(bars_dir(data_dir), manifest)


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


def sync_warmup(
    symbols: Iterable[str],
    *,
    provider: BarsProvider | None = None,
    settings: LabSettings | None = None,
    data_dir: Path | str | None = None,
    force: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    """Download warm-up bars (``warmup.start`` .. ``warmup.overlap_end``) for symbols not yet cached.

    A one-time fetch: unlike the Alpaca cache it never needs refreshing, since
    every load rescales it to Alpaca's current adjusted prices.
    """
    s = settings or load_lab_settings()
    w = s.data.warmup
    ceiling = data_ceiling(s, today=today)
    if w.overlap_end > ceiling:
        raise ValueError(f"warm-up overlap ends {w.overlap_end}, past this machine's data ceiling {ceiling}")
    prov = provider or YFinanceBarsProvider()
    wanted = list(dict.fromkeys(str(x).strip().upper() for x in symbols if str(x).strip()))
    wdir = warmup_dir(data_dir)
    manifest = read_warmup_manifest(data_dir)
    known: dict[str, Any] = manifest.setdefault("tickers", {})
    missing: dict[str, Any] = manifest.setdefault("missing", {})
    span = {"from": w.start.isoformat(), "through": w.overlap_end.isoformat()}

    def _current(sym: str) -> bool:
        entry = known.get(sym) or missing.get(sym)
        return bool(entry) and all(entry.get(k) == v for k, v in span.items())

    todo = wanted if force else [t for t in wanted if not _current(t)]
    fetched = prov.fetch(todo, w.start, w.overlap_end) if todo else {}
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lo, hi = pd.Timestamp(w.start), pd.Timestamp(w.overlap_end)
    for sym in todo:
        df = fetched.get(sym)
        df = None if df is None else df.loc[(df.index >= lo) & (df.index <= hi)]
        if df is None or df.empty:
            known.pop(sym, None)
            missing[sym] = {**span, "synced_at": stamp}
            continue
        save_bars(sym, df, wdir)
        missing.pop(sym, None)
        known[sym] = {
            **span,
            "first": str(df.index.min().date()),
            "last": str(df.index.max().date()),
            "rows": int(len(df)),
            "synced_at": stamp,
        }
    manifest.update({"source": prov.source, "start": span["from"], "overlap_end": span["through"]})
    _write_manifest_in(wdir, manifest)
    return {
        "warmup": span,
        "requested": len(wanted),
        "fetched": sorted(t for t in todo if t in known),
        "missing": sorted(t for t in todo if t in missing),
        "already_current": len(wanted) - len(todo),
        "cache_dir": str(wdir),
    }


def splice_warmup(
    bars: pd.DataFrame, warm: pd.DataFrame, settings: LabSettings
) -> tuple[pd.DataFrame, str | None]:
    """``bars`` with its warm-up rows in front, or ``bars`` unchanged and the reason why not.

    The shared days (``data.start`` .. ``overlap_end``) only line the sources up:
    warm-up prices are scaled by the median Alpaca / yfinance close ratio there,
    and the sources must agree (every day's ratio near the median, volumes
    alike). Only warm-up rows dated before both ``data.start`` and the first
    Alpaca bar are added; volume is not scaled (both sources split-adjust it).
    """
    w = settings.data.warmup
    lo, hi = pd.Timestamp(settings.data.start), pd.Timestamp(w.overlap_end)
    a = bars.loc[(bars.index >= lo) & (bars.index <= hi)]
    y = warm.loc[(warm.index >= lo) & (warm.index <= hi)]
    shared = a.index.intersection(y.index)
    if len(shared) < w.min_overlap_days:
        return bars, f"{len(shared)} shared days with Alpaca (needs {w.min_overlap_days})"
    ratio = (a.loc[shared, "Close"] / y.loc[shared, "Close"]).replace([np.inf, -np.inf], np.nan)
    k = float(ratio.median())
    if ratio.isna().any() or not (np.isfinite(k) and k > 0):
        return bars, "no usable close ratio on the shared days"
    off = float((ratio / k - 1.0).abs().max())
    if off > w.price_tolerance:
        return bars, f"closes disagree by up to {off:.2%} after scaling (limit {w.price_tolerance:.2%})"
    vol = (a.loc[shared, "Volume"] / y.loc[shared, "Volume"]).replace([np.inf, -np.inf], np.nan).dropna()
    vk = float(vol.median()) if len(vol) else float("nan")
    if not (np.isfinite(vk) and 1.0 / w.volume_ratio_max <= vk <= w.volume_ratio_max):
        return bars, f"volumes disagree (median Alpaca / yfinance ratio {vk:.2f}, limit {w.volume_ratio_max}x)"
    cut = min(lo, bars.index.min())
    early = warm.loc[(warm.index >= pd.Timestamp(w.start)) & (warm.index < cut)]
    if early.empty:
        return bars, None
    early = early.assign(**{c: early[c] * k for c in ("Open", "High", "Low", "Close")})
    return pd.concat([early, bars]), None


def load_lab_bars(
    symbols: Iterable[str],
    *,
    settings: LabSettings | None = None,
    data_dir: Path | str | None = None,
    today: date | None = None,
) -> dict[str, pd.DataFrame]:
    """Cached bars for symbols, with every row past the data ceiling dropped.

    Names whose cached warm-up bars agree with Alpaca get them in front
    (``splice_warmup``); a name with no Alpaca bars is left out either way.
    """
    s = settings or load_lab_settings()
    ceiling = data_ceiling(s, today=today)
    out: dict[str, pd.DataFrame] = {}
    for raw in symbols:
        sym = str(raw).strip().upper()
        df = load_bars(sym, bars_dir(data_dir))
        if df is None:
            continue
        df = _clip(df, ceiling)
        if df.empty:
            continue
        warm = load_bars(sym, warmup_dir(data_dir))
        if warm is not None and not warm.empty:
            df, _ = splice_warmup(df, warm, s)
        out[sym] = df
    return out


def warmup_status(
    *,
    settings: LabSettings | None = None,
    data_dir: Path | str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Warm-up cache summary, and the cached names whose sources disagree (they start late)."""
    s = settings or load_lab_settings()
    ceiling = data_ceiling(s, today=today)
    manifest = read_warmup_manifest(data_dir)
    tickers = manifest.get("tickers") or {}
    rejected: dict[str, str] = {}
    for sym in sorted(tickers):
        alpaca, warm = load_bars(sym, bars_dir(data_dir)), load_bars(sym, warmup_dir(data_dir))
        if alpaca is None or warm is None or alpaca.empty or warm.empty:
            continue
        _, reason = splice_warmup(_clip(alpaca, ceiling), warm, s)
        if reason:
            rejected[sym] = reason
    return {
        "cache_dir": str(warmup_dir(data_dir)),
        "source": manifest.get("source"),
        "start": manifest.get("start"),
        "overlap_end": manifest.get("overlap_end"),
        "tickers": len(tickers),
        "missing": sorted(manifest.get("missing") or {}),
        "rejected": rejected,
    }


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
