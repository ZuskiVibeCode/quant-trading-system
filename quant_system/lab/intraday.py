"""Lab intraday bars: Alpaca 15m bars (regular hours), the bar sizes built from them, and coverage.

Only 15m bars are downloaded. Alpaca's own 1h bars start on the clock hour, so
the 09:00 bar mixes 09:00-09:30 pre-market trades into the session's first
hour; 15m bars line up with the 09:30 open. 30m / 1h / 2h bars are built here
from the 15m bars, starting at each session's open (1h: 09:30, 10:30 ... 15:30,
the last one 30 minutes long).

Regular hours come from Alpaca's market calendar (cached next to the bars), so
half days end at 13:00 and after-close prints are dropped. A bar is kept when it
starts at or after the session's open and before its close. The closing auction
prints at 16:00 and lands in Alpaca's 16:00 bar, so the last 15m close of a
session is the last trade before the close, not the official close: the price a
signal could see at that moment.

Storage: ``lab_data_dir()/bars_15m/<SYM>.csv.gz`` (one file per name, bar start
in New York time), ``_calendar.csv`` and ``_manifest.json``. Nothing past the
machine's data ceiling is downloaded or loaded. The engine never holds every
name's frames at once: ``build_panels`` reads one name at a time into float32
bars x names arrays, and ``IntradaySource`` keeps one bar size cached.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time as _time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import numpy as np
import pandas as pd

from quant_system.backtest.cache import _safe_filename
from quant_system.lab.data import MARKET_TZ, load_lab_bars
from quant_system.lab.settings import LabSettings, data_ceiling, lab_data_dir, load_lab_settings

BASE_TIMEFRAME = "15m"
TIMEFRAME_MINUTES = {"15m": 15, "30m": 30, "1h": 60, "2h": 120}
MANIFEST_NAME = "_manifest.json"
CALENDAR_NAME = "_calendar.csv"
COVERAGE_NAME = "_coverage.json"
FIELDS = ("Open", "High", "Low", "Close", "Volume")
_RAW_COLUMNS = {"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"}


def intraday_dir(data_dir: Path | str | None = None) -> Path:
    """``<lab data dir>/bars_15m``; ``data_dir`` overrides the lab data root."""
    return (Path(data_dir) if data_dir else lab_data_dir()) / "bars_15m"


def bar_file(symbol: str, data_dir: Path | str | None = None) -> Path:
    return intraday_dir(data_dir) / f"{_safe_filename(symbol)}.csv.gz"


def minutes(timeframe: str) -> int:
    if timeframe not in TIMEFRAME_MINUTES:
        raise ValueError(f"unknown timeframe {timeframe!r}; expected one of {', '.join(TIMEFRAME_MINUTES)}")
    return TIMEFRAME_MINUTES[timeframe]


# -- Alpaca -----------------------------------------------------------------


class AlpacaIntradayProvider:
    """15m bars and the market calendar from Alpaca (paper keys work).

    The feed is never switched: if Alpaca refuses the configured feed, the
    sync stops with Alpaca's error.
    """

    source = "alpaca"

    def __init__(self, client: Any | None = None, trading: Any | None = None, *,
                 feed: str = "sip", adjustment: str = "all"):
        self._client = client
        self._trading = trading
        self.feed = feed
        self.adjustment = adjustment

    def _creds(self) -> dict[str, Any]:
        from quant_system.config.loader import alpaca_credentials

        creds = alpaca_credentials()
        if not creds["configured"]:
            raise RuntimeError(
                "Alpaca keys not set (ALPACA_API_KEY / ALPACA_SECRET_KEY); paper keys work for market data"
            )
        return creds

    def _data(self) -> Any:
        if self._client is None:
            from alpaca.data.historical import StockHistoricalDataClient

            c = self._creds()
            # raw_data: plain dicts, not one pydantic model per bar (memory).
            self._client = StockHistoricalDataClient(c["api_key"], c["secret_key"], raw_data=True)
        return self._client

    def calendar(self, start: date, end: date) -> pd.DataFrame:
        """Sessions start..end: index = session date, columns open / close (New York time)."""
        from alpaca.trading.requests import GetCalendarRequest

        if self._trading is None:
            from alpaca.trading.client import TradingClient

            c = self._creds()
            self._trading = TradingClient(c["api_key"], c["secret_key"], paper=True)
        rows = self._trading.get_calendar(GetCalendarRequest(start=start, end=end))
        return calendar_frame(
            [(pd.Timestamp(r.date), pd.Timestamp(r.open), pd.Timestamp(r.close)) for r in rows]
        )

    def fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """All 15m bars (every session hour) start..end, indexed by bar start in New York time."""
        from alpaca.common.exceptions import APIError
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        client = self._data()
        parts = []
        # One calendar year per request keeps each response small.
        for year in range(start.year, end.year + 1):
            lo, hi = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame(15, TimeFrameUnit.Minute),
                start=datetime.combine(lo, time.min, MARKET_TZ),
                end=datetime.combine(hi, time.max, MARKET_TZ),
                adjustment=Adjustment(self.adjustment),
                feed=DataFeed(self.feed),
            )
            try:
                raw = client.get_stock_bars(req)
            except APIError as exc:
                raise RuntimeError(
                    f"Alpaca refused {self.feed} 15m bars for {symbol} {lo}..{hi}: {exc}. "
                    "Not switching feeds; stopping."
                ) from None
            rows = (raw or {}).get(symbol) or []
            if rows:
                parts.append(raw_bars_frame(rows))
        if not parts:
            return pd.DataFrame(columns=list(FIELDS), index=pd.DatetimeIndex([]), dtype=float)
        out = pd.concat(parts)
        return out[~out.index.duplicated(keep="last")].sort_index()


def raw_bars_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Alpaca raw bars (``t`` in UTC) → Open..Volume indexed by bar start, New York time, tz-naive."""
    df = pd.DataFrame(rows)
    idx = pd.DatetimeIndex(pd.to_datetime(df["t"], utc=True)).tz_convert(MARKET_TZ).tz_localize(None)
    out = df.rename(columns=_RAW_COLUMNS)[list(FIELDS)].astype(float)
    out.index = idx
    return out


def calendar_frame(rows: Iterable[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]]) -> pd.DataFrame:
    """(session, open, close) rows → the calendar frame; open / close carry the session's date."""
    out = []
    for day, op, cl in rows:
        d = pd.Timestamp(day).normalize()
        out.append((d, d + (pd.Timestamp(op) - pd.Timestamp(op).normalize()),
                    d + (pd.Timestamp(cl) - pd.Timestamp(cl).normalize())))
    cal = pd.DataFrame(out, columns=["session", "open", "close"]).set_index("session").sort_index()
    return cal[~cal.index.duplicated(keep="last")]


# -- cache ------------------------------------------------------------------


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def _write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_manifest(data_dir: Path | str | None = None) -> dict[str, Any]:
    return _read_json(intraday_dir(data_dir) / MANIFEST_NAME, {"tickers": {}, "missing": {}})


def save_calendar(cal: pd.DataFrame, data_dir: Path | str | None = None) -> None:
    path = intraday_dir(data_dir) / CALENDAR_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    out = pd.DataFrame({
        "session": cal.index.strftime("%Y-%m-%d"),
        "open": cal["open"].dt.strftime("%H:%M"),
        "close": cal["close"].dt.strftime("%H:%M"),
    })
    tmp = path.parent / f".{path.name}.tmp"
    out.to_csv(tmp, index=False)
    os.replace(tmp, path)


def load_calendar(data_dir: Path | str | None = None) -> pd.DataFrame | None:
    path = intraday_dir(data_dir) / CALENDAR_NAME
    if not path.is_file():
        return None
    raw = pd.read_csv(path, dtype=str)
    day = pd.to_datetime(raw["session"], format="%Y-%m-%d")
    return calendar_frame(
        zip(day, day + pd.to_timedelta(raw["open"] + ":00"), day + pd.to_timedelta(raw["close"] + ":00"))
    )


def save_intraday(symbol: str, df: pd.DataFrame, data_dir: Path | str | None = None) -> Path:
    path = bar_file(symbol, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df[list(FIELDS)].copy()
    out[["Open", "High", "Low", "Close"]] = out[["Open", "High", "Low", "Close"]].round(4)
    out["Volume"] = out["Volume"].round().astype("int64")
    out.index = out.index.strftime("%Y-%m-%d %H:%M")
    tmp = path.parent / f".{path.stem}.tmp.gz"
    out.to_csv(tmp, index_label="Timestamp", compression="gzip")
    os.replace(tmp, path)
    return path


def load_intraday(symbol: str, data_dir: Path | str | None = None) -> pd.DataFrame | None:
    """One name's cached 15m bars, or None."""
    path = bar_file(symbol, data_dir)
    if not path.is_file():
        return None
    df = pd.read_csv(path, compression="gzip")
    df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("Timestamp"), format="%Y-%m-%d %H:%M"))
    return df[list(FIELDS)].astype(float)


def regular_hours(df: pd.DataFrame, calendar: pd.DataFrame) -> pd.DataFrame:
    """Bars that start inside a session (open <= start < close); everything else dropped."""
    if df.empty:
        return df
    day = df.index.normalize()
    op = calendar["open"].reindex(day).to_numpy()
    cl = calendar["close"].reindex(day).to_numpy()
    ts = df.index.to_numpy()
    keep = ~pd.isna(op) & (ts >= op) & (ts < cl)
    return df.loc[keep]


def _clip(df: pd.DataFrame, ceiling: date) -> pd.DataFrame:
    if df.empty:
        return df
    return df.loc[pd.DatetimeIndex(df.index).normalize() <= pd.Timestamp(ceiling)]


def sync_intraday(
    symbols: Iterable[str],
    *,
    provider: Any | None = None,
    settings: LabSettings | None = None,
    data_dir: Path | str | None = None,
    force: bool = False,
    today: date | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Download regular-hours 15m bars (``data.start`` .. ceiling) for names not yet current.

    Whole history per name, like the daily cache (adjusted prices change after
    every split or dividend). The manifest is written after each name, so an
    interrupted sync picks up where it stopped.
    """
    s = settings or load_lab_settings()
    ceiling = data_ceiling(s, today=today)
    start = s.data.start
    prov = provider or AlpacaIntradayProvider(feed=s.data.feed, adjustment=s.data.adjustment)
    wanted = list(dict.fromkeys(str(x).strip().upper() for x in symbols if str(x).strip()))
    idir = intraday_dir(data_dir)
    manifest = read_manifest(data_dir)
    known: dict[str, Any] = manifest.setdefault("tickers", {})
    missing: dict[str, Any] = manifest.setdefault("missing", {})

    def _current(sym: str) -> bool:
        entry = known.get(sym) or missing.get(sym)
        return bool(entry) and entry.get("fetched_through", "") >= ceiling.isoformat()

    todo = wanted if force else [t for t in wanted if not _current(t)]
    cal = load_calendar(data_dir)
    if todo or cal is None:
        cal = prov.calendar(start, ceiling)
        cal = cal.loc[(cal.index >= pd.Timestamp(start)) & (cal.index <= pd.Timestamp(ceiling))]
        save_calendar(cal, data_dir)
    manifest.update({
        "source": prov.source, "feed": s.data.feed, "adjustment": s.data.adjustment,
        "timeframe": BASE_TIMEFRAME, "start": start.isoformat(), "ceiling": ceiling.isoformat(),
    })
    for n, sym in enumerate(todo, 1):
        raw = prov.fetch(sym, start, ceiling)
        df = _clip(regular_hours(raw, cal), ceiling)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if df.empty:
            known.pop(sym, None)
            missing[sym] = {"fetched_through": ceiling.isoformat(), "synced_at": stamp}
        else:
            save_intraday(sym, df, data_dir)
            missing.pop(sym, None)
            known[sym] = {
                "first": str(df.index.min()), "last": str(df.index.max()), "rows": int(len(df)),
                "outside_hours_dropped": int(len(raw) - len(df)),
                "fetched_through": ceiling.isoformat(), "synced_at": stamp,
            }
        _write_json(idir / MANIFEST_NAME, manifest)
        if progress:
            progress(f"{n}/{len(todo)} {sym}: {known.get(sym, {}).get('rows', 0)} bars")
    _write_json(idir / MANIFEST_NAME, manifest)
    return {
        "ceiling": ceiling.isoformat(),
        "timeframe": BASE_TIMEFRAME,
        "requested": len(wanted),
        "fetched": sorted(t for t in todo if t in known),
        "missing": sorted(t for t in todo if t in missing),
        "already_current": len(wanted) - len(todo),
        "cache_dir": str(idir),
    }


# -- bar sizes and the session grid ----------------------------------------


def bar_slots(calendar: pd.DataFrame, timeframe: str, start: date, end: date) -> pd.DataFrame:
    """Every scheduled bar start in sessions start..end, with its place in the session.

    Columns: ``session`` (date), ``bar_of_day`` (0 = first), ``bars_left``
    (0 = last), ``minute`` (minutes after the open). The last bar of a session
    may be short (1h: 15:30-16:00).
    """
    step = minutes(timeframe)
    cal = calendar.loc[(calendar.index >= pd.Timestamp(start)) & (calendar.index <= pd.Timestamp(end))]
    span = ((cal["close"] - cal["open"]) / pd.Timedelta(minutes=1)).to_numpy()
    counts = np.ceil(span / step).astype(int)
    total = int(counts.sum())
    sess = np.repeat(np.arange(len(cal)), counts)
    first = np.repeat(np.cumsum(counts) - counts, counts)
    k = np.arange(total) - first
    opens = cal["open"].to_numpy()[sess]
    idx = pd.DatetimeIndex(opens + (k * step).astype("timedelta64[m]"))
    return pd.DataFrame(
        {
            "session": cal.index.to_numpy()[sess],
            "bar_of_day": k,
            "bars_left": counts[sess] - 1 - k,
            "minute": k * step,
        },
        index=idx,
    )


def resample(df15: pd.DataFrame, calendar: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """15m bars → ``timeframe`` bars starting at each session's open.

    A bar is built only from the 15m bars inside it: open = first, high = max,
    low = min, close = last, volume = sum. Missing 15m bars are skipped (the
    bar opens at the first trade inside it); a bar with none is absent.
    """
    if timeframe == BASE_TIMEFRAME or df15.empty:
        return df15
    step = pd.Timedelta(minutes=minutes(timeframe))
    op = pd.DatetimeIndex(calendar["open"].reindex(df15.index.normalize()).to_numpy())
    bucket = op + ((df15.index - op) // step) * step
    g = df15.groupby(bucket)
    out = pd.DataFrame({
        "Open": g["Open"].first(), "High": g["High"].max(), "Low": g["Low"].min(),
        "Close": g["Close"].last(), "Volume": g["Volume"].sum(),
    })
    out.index = pd.DatetimeIndex(out.index)
    return out


# -- engine panels ----------------------------------------------------------


@dataclass
class IntradayPanels:
    """One bar size for a set of names: float32 arrays, rows = scheduled bars, NaN = no bar.

    Columns are ``symbols`` in sorted order; names with no intraday bars stay as
    all-NaN columns and are listed in ``missing``.
    """

    timeframe: str
    index: pd.DatetimeIndex
    symbols: list[str]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    bench_close: np.ndarray  # float64, the benchmark's close on each bar
    session: np.ndarray  # row -> position in ``sessions``
    sessions: pd.DatetimeIndex
    bar_of_day: np.ndarray
    bars_left: np.ndarray
    minute: np.ndarray
    missing: list[str] = field(default_factory=list)

    @property
    def nbytes(self) -> int:
        return sum(a.nbytes for a in (self.open, self.high, self.low, self.close, self.volume))


def build_panels(
    symbols: Iterable[str],
    timeframe: str,
    *,
    bench: str,
    calendar: pd.DataFrame,
    loader: Callable[[str], pd.DataFrame | None],
    start: date,
    ceiling: date,
) -> IntradayPanels:
    """Read each name's 15m bars (one at a time), build ``timeframe`` bars, fill the panels."""
    names = sorted(dict.fromkeys(str(x).strip().upper() for x in symbols if str(x).strip()))
    slots = bar_slots(calendar, timeframe, start, ceiling)
    index = slots.index
    n, m = len(index), len(names)
    arrays = {f: np.full((n, m), np.nan, dtype=np.float32) for f in FIELDS}
    missing: list[str] = []

    def _bars(sym: str) -> pd.DataFrame | None:
        df = loader(sym)
        if df is None or df.empty:
            return None
        df = _clip(df, ceiling)
        df = df.loc[df.index.normalize() >= pd.Timestamp(start)]
        return resample(df, calendar, timeframe) if not df.empty else None

    for j, sym in enumerate(names):
        bars = _bars(sym)
        if bars is None:
            missing.append(sym)
            continue
        rows = index.get_indexer(bars.index)
        ok = rows >= 0
        for f in FIELDS:
            arrays[f][rows[ok], j] = bars[f].to_numpy()[ok]
        del bars
    bench_bars = _bars(bench.upper())
    if bench_bars is None:
        raise ValueError(f"benchmark {bench} has no intraday bars; run `lab data intraday`")
    sessions = pd.DatetimeIndex(slots["session"].unique())
    return IntradayPanels(
        timeframe=timeframe,
        index=index,
        symbols=names,
        open=arrays["Open"], high=arrays["High"], low=arrays["Low"],
        close=arrays["Close"], volume=arrays["Volume"],
        bench_close=bench_bars["Close"].reindex(index).to_numpy(dtype=float),
        session=sessions.get_indexer(pd.DatetimeIndex(slots["session"])).astype(np.int32),
        sessions=sessions,
        bar_of_day=slots["bar_of_day"].to_numpy(dtype=np.int16),
        bars_left=slots["bars_left"].to_numpy(dtype=np.int16),
        minute=slots["minute"].to_numpy(dtype=np.int16),
        missing=missing,
    )


@contextmanager
def intraday_run_lock(
    data_dir: Path | str | None = None, *, wait: Callable[[], None] | None = None
) -> Iterator[None]:
    """One intraday run at a time on this machine (Grok's has little free memory, no swap).

    An OS lock on ``<lab data dir>/intraday.lock``: a second `lab run` / `lab robust`
    / `lab nominate` of an intraday spec waits for the first. The OS drops the
    lock when the process ends, so a crash never leaves it stuck. ``wait`` is
    called once if the lock is busy.
    """
    path = (Path(data_dir) if data_dir else lab_data_dir()) / "intraday.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                if wait:
                    wait()
                while True:
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        _time.sleep(2)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if wait:
                    wait()
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class IntradaySource:
    """Builds ``IntradayPanels`` on request and keeps one bar size cached.

    A request for names already in the cached panels at the same bar size
    reuses them (the engine masks out the rest), so a robustness run reads
    each name's file once per bar size. Asking for another bar size frees the
    cached one first. Tests pass ``loader`` and ``calendar`` instead of a cache.
    """

    def __init__(
        self,
        *,
        settings: LabSettings | None = None,
        data_dir: Path | str | None = None,
        today: date | None = None,
        loader: Callable[[str], pd.DataFrame | None] | None = None,
        calendar: pd.DataFrame | None = None,
    ):
        self.settings = settings or load_lab_settings()
        self.data_dir = data_dir
        self.ceiling = data_ceiling(self.settings, today=today)
        self._loader = loader or (lambda sym: load_intraday(sym, data_dir))
        self._calendar = calendar
        self._cached: IntradayPanels | None = None

    @property
    def calendar(self) -> pd.DataFrame:
        if self._calendar is None:
            cal = load_calendar(self.data_dir)
            if cal is None:
                raise ValueError("no intraday calendar in the lab cache; run `lab data intraday`")
            self._calendar = cal
        return self._calendar

    def panels(self, timeframe: str, symbols: Iterable[str], *, bench: str) -> IntradayPanels:
        want = {str(x).strip().upper() for x in symbols if str(x).strip()}
        c = self._cached
        if c is not None and c.timeframe == timeframe and want <= set(c.symbols):
            return c
        self._cached = None  # free the old panels before building new ones
        self._cached = build_panels(
            want, timeframe, bench=bench, calendar=self.calendar, loader=self._loader,
            start=self.settings.data.start, ceiling=self.ceiling,
        )
        return self._cached


# -- coverage ----------------------------------------------------------------


def _longest_run(flags: np.ndarray) -> int:
    best = run = 0
    for f in flags:
        run = run + 1 if f else 0
        best = max(best, run)
    return best


def ticker_coverage(
    df15: pd.DataFrame | None,
    daily: pd.DataFrame | None,
    calendar: pd.DataFrame,
    start: date,
    ceiling: date,
    cov: Any,
) -> dict[str, Any]:
    """Coverage of one name at 15m and 1h, against the calendar and its daily bars.

    Expected sessions: calendar sessions on which the daily cache has a bar
    (every session from ``start`` when the name has no daily bars).
    """
    cal = calendar.loc[(calendar.index >= pd.Timestamp(start)) & (calendar.index <= pd.Timestamp(ceiling))]
    if daily is not None and not daily.empty:
        days = cal.index.intersection(daily.index.normalize())
    else:
        days = cal.index
    row: dict[str, Any] = {"sessions_expected": int(len(days))}
    if df15 is None or df15.empty:
        row.update({"first": None, "last": None, "flags": ["no intraday bars"]})
        return row
    df15 = _clip(df15, ceiling)
    row["first"], row["last"] = str(df15.index.min()), str(df15.index.max())
    sub = cal.loc[days]
    flags: list[str] = []
    by_day = df15.groupby(df15.index.normalize())
    per_day = by_day.size().reindex(days, fill_value=0).to_numpy()
    for tf in ("15m", "1h"):
        slots = bar_slots(sub, tf, start, ceiling)
        have = resample(df15, calendar, tf).index.intersection(slots.index)
        expected = int(len(slots))
        share = 1.0 - len(have) / expected if expected else 0.0
        row[tf] = {"bars_expected": expected, "bars": int(len(have)), "missing_share": share}
        if share > cov.max_missing_share:
            flags.append(f"{tf}: {share:.1%} of bars missing (limit {cov.max_missing_share:.0%})")
    empty = per_day == 0
    row["empty_sessions"] = int(empty.sum())
    row["longest_empty_run"] = _longest_run(empty)
    if row["empty_sessions"] > cov.max_empty_sessions:
        flags.append(f"{row['empty_sessions']} sessions with no bars (limit {cov.max_empty_sessions})")
    if daily is not None and not daily.empty:
        d = daily.loc[daily.index.isin(days)]
        first_open = by_day["Open"].first().reindex(d.index)
        last_close = by_day["Close"].last().reindex(d.index)
        vol = by_day["Volume"].sum().reindex(d.index)
        mism = 0
        for name, ours, theirs in (("open", first_open, d["Open"]), ("close", last_close, d["Close"])):
            ratio = (ours / theirs).replace([np.inf, -np.inf], np.nan).dropna()
            k = float(ratio.median()) if len(ratio) else float("nan")
            off = (ratio / k - 1.0).abs() if math.isfinite(k) and k > 0 else ratio * np.nan
            bad = int((off > cov.price_tolerance).sum())
            row[f"{name}_ratio_median"] = k
            row[f"{name}_mismatch_days"] = bad
            mism += bad
        if mism > cov.max_mismatch_days:
            flags.append(f"{mism} open/close days off the daily bar by over {cov.price_tolerance:.0%}")
        vr = (vol / d["Volume"]).replace([np.inf, -np.inf], np.nan).dropna()
        row["volume_ratio_median"] = float(vr.median()) if len(vr) else None
    else:
        flags.append("no daily bars to compare")
    row["flags"] = flags
    return row


def intraday_coverage(
    symbols: Iterable[str],
    *,
    settings: LabSettings | None = None,
    data_dir: Path | str | None = None,
    today: date | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Per-name coverage at 15m and 1h; the full table goes to ``_coverage.json`` in the cache."""
    s = settings or load_lab_settings()
    ceiling = data_ceiling(s, today=today)
    cal = load_calendar(data_dir)
    if cal is None:
        raise ValueError("no intraday calendar in the lab cache; run `lab data intraday`")
    names = list(dict.fromkeys(str(x).strip().upper() for x in symbols if str(x).strip()))
    rows: dict[str, Any] = {}
    for sym in names:
        daily = load_lab_bars([sym], settings=s, data_dir=data_dir, today=today).get(sym)
        if daily is not None:
            daily = daily.loc[daily.index >= pd.Timestamp(s.data.start)]
        rows[sym] = ticker_coverage(load_intraday(sym, data_dir), daily, cal, s.data.start, ceiling,
                                    s.data.intraday.coverage)
    flagged = {k: v["flags"] for k, v in rows.items() if v["flags"]}

    def _median(tf: str) -> float | None:
        vals = [v[tf]["missing_share"] for v in rows.values() if tf in v]
        return float(np.median(vals)) if vals else None

    def _worst(tf: str) -> list[dict[str, Any]]:
        vals = sorted(((v[tf]["missing_share"], k) for k, v in rows.items() if tf in v), reverse=True)
        return [{"symbol": k, "missing_share": x} for x, k in vals[:5]]

    report = {
        "ceiling": ceiling.isoformat(),
        "cache_dir": str(intraday_dir(data_dir)),
        "sessions": int(((cal.index >= pd.Timestamp(s.data.start)) & (cal.index <= pd.Timestamp(ceiling))).sum()),
        "early_closes": [str(d.date()) for d, c in cal["close"].items()
                         if pd.Timestamp(s.data.start) <= d <= pd.Timestamp(ceiling) and c.time() != time(16, 0)],
        "names": len(rows),
        "no_bars": sorted(k for k, v in rows.items() if v.get("first") is None),
        "median_missing_share": {tf: _median(tf) for tf in ("15m", "1h")},
        "most_missing": {tf: _worst(tf) for tf in ("15m", "1h")},
        "flagged": flagged,
        "thresholds": s.data.intraday.coverage.model_dump(mode="json"),
    }
    if write:
        path = intraday_dir(data_dir) / COVERAGE_NAME
        _write_json(path, {**report, "tickers": rows})
        report["full_report"] = str(path)
    return report


def intraday_status(
    *,
    settings: LabSettings | None = None,
    data_dir: Path | str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Cache summary for `lab data status`, flagging any file holding bars past the ceiling."""
    ceiling = data_ceiling(settings or load_lab_settings(), today=today)
    manifest = read_manifest(data_dir)
    tickers = manifest.get("tickers") or {}
    return {
        "cache_dir": str(intraday_dir(data_dir)),
        "timeframe": manifest.get("timeframe"),
        "feed": manifest.get("feed"),
        "tickers": len(tickers),
        "missing": sorted(manifest.get("missing") or {}),
        "rows_past_ceiling": sorted(t for t, e in tickers.items() if e.get("last", "")[:10] > ceiling.isoformat()),
        "stale": sorted(t for t, e in tickers.items() if e.get("fetched_through", "") < ceiling.isoformat()),
    }
