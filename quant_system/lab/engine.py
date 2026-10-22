"""Spec engine: run a StrategySpec over one window with the lab fill and cost model.

Each session t of the window:

1. Open: fill yesterday's orders at today's open, sells first, then buys in rank
   order. Slippage is the name's bucket as of the signal day. A name with no
   bar today doesn't fill (an exit is re-signalled tonight if still true).
2. Close: mark positions (a name with no bar today keeps its last close).
3. Signals at the close, for tomorrow's open (none on the window's last day):
   - exit: held names whose exit rule is true;
   - entry: names not held, entry rule true, with a bar today and a liquidity
     bucket (not tradable = never entered), best ``rank`` first, while slots
     remain under ``max_positions``.

Sizing: each entry buys ``min(1 / max_positions, max_weight)`` of equity at
the signal close, capped by cash. Long only. Like the live PortfolioManager, the
20% cap applies when buying; a position that grows past it is not trimmed.
Exits pay slippage too; a held name that has fallen below the lowest liquidity
bucket exits at the costliest bucket's rate. Positions still open at the end
are marked at the last close with no exit cost, the same as the benchmarks.

Intraday specs (``timeframe``, ``run_spec_intraday``) run the same loop over
bars instead of sessions: a signal at a bar's close fills at the next bar's
open, which after a session's last bar is the next session's open (the
overnight gap is part of the trade). ``bars_held`` counts bars. Slippage comes
from ``costs.intraday``, by the daily median dollar volume as of the previous
session's close. Results are per session (equity at each session's last bar),
so every metric, benchmark and the trial log stay on daily returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from quant_system.lab.costs import median_dollar_volume, slippage_bps
from quant_system.lab.data import bars_panel
from quant_system.lab.indicators import get_indicator
from quant_system.lab.rules import Evaluator
from quant_system.lab.settings import LabCostSettings
from quant_system.lab.spec import StrategySpec
from quant_system.lab.windows import ResolvedWindow

if TYPE_CHECKING:
    from quant_system.lab.intraday import IntradayPanels

_FIELDS = {"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}


@dataclass
class EngineResult:
    returns: pd.Series  # daily, window sessions
    weights: pd.DataFrame  # end-of-day weight per name
    traded: pd.Series  # daily |notional traded| / prior equity
    trades: pd.DataFrame  # one row per fill
    contributions: pd.Series  # P&L per name, as a fraction of starting equity
    symbols: list[str]
    missing: list[str]  # universe names with no bars
    first_trade_date: str | None = None
    notes: list[str] = field(default_factory=list)
    # Share of equity in positions right after each open's fills, valued at the open.
    open_exposure: pd.Series | None = None
    # Intraday only. Per bar: share invested right after the open's fills, and at the close.
    bar_exposure: pd.DataFrame | None = None
    # Intraday only. Per session: previous close to the first bar's open (before fills),
    # then the rest of the session; (1 + overnight) x (1 + intraday) = 1 + returns.
    overnight: pd.Series | None = None
    intraday: pd.Series | None = None

    @property
    def trade_count(self) -> int:
        return int(len(self.trades))


def _through(bars: dict[str, pd.DataFrame], end) -> dict[str, pd.DataFrame]:
    cut = pd.Timestamp(end)
    out = {s: df.loc[df.index <= cut] for s, df in bars.items()}
    return {s: df for s, df in out.items() if not df.empty}


def indicator_panels(
    spec: StrategySpec,
    bars: dict[str, pd.DataFrame],
    bench_close: pd.Series,
    calendar: pd.DatetimeIndex,
) -> dict[str, np.ndarray]:
    """Each spec indicator, computed per name on its own sessions, as sessions x names."""
    out: dict[str, np.ndarray] = {}
    frames = {
        s: df.assign(BenchClose=bench_close.reindex(df.index)) for s, df in bars.items()
    }
    for name in spec.indicators:
        ind = get_indicator(str(spec.indicators[name]["fn"]))
        args = spec.indicator_args(name)
        panel = pd.DataFrame({s: ind(f, **args) for s, f in frames.items()}).reindex(calendar)
        out[name] = panel.to_numpy(dtype=float)
    return out


@dataclass
class _Sim:
    equity: np.ndarray  # at each row's close
    weights: np.ndarray  # end-of-row weights, only on rows in ``weight_rows``
    traded: np.ndarray
    open_exposure: np.ndarray  # right after the open's fills
    close_exposure: np.ndarray
    pre_open_equity: np.ndarray  # at the open, before its fills
    trades: list[dict[str, Any]]
    pnl: np.ndarray


def _simulate(
    spec: StrategySpec,
    calendar: pd.DatetimeIndex,
    pos: np.ndarray,
    symbols: list[str],
    open_: np.ndarray,
    mark: np.ndarray,
    bps: np.ndarray,
    worst_bps: np.ndarray,
    entry: np.ndarray,
    rank: np.ndarray,
    exit_eval: Evaluator,
    exit_full: np.ndarray | None,
    weight_rows: np.ndarray | None = None,
    carry_exits: bool = False,
) -> _Sim:
    """The fill loop over calendar rows ``pos`` (sessions, or bars for intraday specs).

    ``bps`` / ``worst_bps`` are per signal row (the row whose close made the
    order). ``weight_rows`` (bool per ``pos`` entry) picks the rows whose
    weights are kept; all rows when None. ``carry_exits``: a sell whose fill
    row has no price stays pending and fills at the next row that has one
    (intraday specs, where a name's skipped session is a run of empty bars);
    otherwise it is dropped and the exit rule decides again. With
    ``carry_exits`` a buy also checks the position cap when it fills, so a
    pending sell keeps its slot until it fills.
    """
    n = len(symbols)
    shares = np.zeros(n)
    cost_basis = np.zeros(n)  # cash paid for the open position
    entered_at = np.full(n, -1)
    pnl = np.zeros(n)
    cash = 1.0
    prev_equity = 1.0
    target_w = spec.sizing.target_weight
    max_pos = spec.sizing.max_positions

    sells: list[int] = []
    buys: list[int] = []
    signal_row = -1
    keep = np.ones(len(pos), dtype=bool) if weight_rows is None else weight_rows
    weight_slot = np.cumsum(keep) - 1
    equity_out = np.empty(len(pos))
    weights_out = np.zeros((int(keep.sum()), n))
    traded_out = np.zeros(len(pos))
    open_exposure_out = np.zeros(len(pos))
    close_exposure_out = np.zeros(len(pos))
    pre_open_out = np.zeros(len(pos))
    trades: list[dict[str, Any]] = []

    for k, i in enumerate(pos):
        day = calendar[i]
        traded = 0.0
        open_px = np.nan_to_num(np.where(np.isnan(open_[i]), mark[i], open_[i]))
        pre_open_out[k] = cash + float((shares * open_px).sum())
        # 1. Open: fill orders from the previous close.
        unfilled: list[int] = []
        for j in sells:
            px_open = open_[i, j]
            if shares[j] <= 0:
                continue
            if np.isnan(px_open):
                if carry_exits:
                    unfilled.append(j)
                continue
            b = bps[signal_row, j]
            b = worst_bps[signal_row] if np.isnan(b) else b
            proceeds = shares[j] * px_open * (1.0 - b / 10_000.0)
            cash += proceeds
            pnl[j] += proceeds - cost_basis[j]
            traded += proceeds
            trades.append({"date": day, "symbol": symbols[j], "side": "sell", "shares": shares[j],
                           "price": px_open, "bps": b, "notional": proceeds})
            shares[j], cost_basis[j], entered_at[j] = 0.0, 0.0, -1
        for j in buys:
            px_open = open_[i, j]
            budget = min(target_w * prev_equity, cash)
            if np.isnan(px_open) or budget <= 1e-9:
                continue
            if carry_exits and int((shares > 0).sum()) >= max_pos:
                continue  # a pending sell still holds its slot until it fills
            b = bps[signal_row, j]
            shares[j] = budget / (px_open * (1.0 + b / 10_000.0))
            cost_basis[j] = budget
            entered_at[j] = i
            cash -= budget
            traded += budget
            trades.append({"date": day, "symbol": symbols[j], "side": "buy", "shares": shares[j],
                           "price": px_open, "bps": b, "notional": budget})
        traded_out[k] = traded / prev_equity
        sells, buys = [], []
        at_open = shares * open_px
        invested = float(at_open.sum())
        open_exposure_out[k] = invested / (cash + invested) if cash + invested > 0 else 0.0

        # 2. Close: mark to market.
        values = shares * np.nan_to_num(mark[i])
        equity = cash + values.sum()
        equity_out[k] = equity
        if keep[k]:
            weights_out[weight_slot[k]] = values / equity if equity > 0 else 0.0
        close_exposure_out[k] = float(values.sum()) / equity if equity > 0 else 0.0
        prev_equity = equity
        if k == len(pos) - 1:
            break

        # 3. Signals for the next row's open.
        held = shares > 0
        if held.any():
            if exit_full is not None:
                ex = exit_full[i]
            else:
                bars_held = np.where(held, i - entered_at + 1, np.nan).astype(float)
                ex = exit_eval.at(i, {"bars_held": bars_held})
            sells = list(np.flatnonzero(held & ex))
        if unfilled:
            sells = sorted(set(sells) | set(unfilled))
        slots = max_pos - (int(held.sum()) - len(sells))
        if slots > 0:
            cand = np.flatnonzero(entry[i] & ~held)
            if len(cand):
                r = np.nan_to_num(rank[i, cand], nan=-np.inf)
                order = np.lexsort((cand, -r))  # best rank first, then symbol order
                buys = list(cand[order][:slots])
        signal_row = i

    for j in np.flatnonzero(shares > 0):
        pnl[j] += shares[j] * mark[pos[-1], j] - cost_basis[j]
    return _Sim(equity_out, weights_out, traded_out, open_exposure_out, close_exposure_out,
                pre_open_out, trades, pnl)


def run_spec(
    spec: StrategySpec,
    bars: dict[str, pd.DataFrame],
    universe: list[str],
    window: ResolvedWindow,
    costs: LabCostSettings,
    *,
    bench: str = "SPY",
) -> EngineResult:
    wanted = [s.upper() for s in dict.fromkeys(universe)]
    clipped = _through(bars, window.end)
    if bench.upper() not in clipped:
        raise ValueError(f"benchmark {bench} bars missing; run `lab data sync`")
    held_bars = {s: clipped[s] for s in wanted if s in clipped}
    missing = [s for s in wanted if s not in clipped]
    if not held_bars:
        raise ValueError("no bars for any name in the universe")
    symbols = sorted(held_bars)
    calendar = bars_panel(clipped, "Close").index
    sessions = window.sessions(calendar)
    if sessions.empty:
        raise ValueError(f"no sessions in {window.name} {window.start}..{window.end}")
    pos = calendar.get_indexer(sessions)

    px = {k: bars_panel(held_bars, col).reindex(index=calendar, columns=symbols) for k, col in _FIELDS.items()}
    env: dict[str, Any] = {k: v.to_numpy(dtype=float) for k, v in px.items()}
    env.update(indicator_panels(spec, {s: held_bars[s] for s in symbols}, clipped[bench.upper()]["Close"], calendar))
    env.update(spec.param_values())

    mdv = median_dollar_volume(px["close"], px["volume"], costs.lookback_days)
    bps = slippage_bps(mdv, costs).to_numpy()
    worst_bps = max(b.bps for b in costs.buckets)
    has_bar = ~np.isnan(env["close"])
    mark = px["close"].ffill().to_numpy()
    open_ = env["open"]

    rules = spec.rules
    entry = Evaluator(rules["entry"], env).full() & has_bar & ~np.isnan(bps)  # broadcasts constants
    shape = env["close"].shape
    if "rank" in rules:
        rank = np.broadcast_to(Evaluator(rules["rank"], env).values(), shape)
    else:
        rank = mdv.to_numpy()  # default: most liquid first
    exit_eval = Evaluator(rules["exit"], env)
    exit_full = None if rules["exit"].uses_state else np.broadcast_to(exit_eval.full(), shape)

    sim = _simulate(
        spec, calendar, pos, symbols, open_, mark, bps, np.full(len(calendar), worst_bps),
        entry, rank, exit_eval, exit_full,
    )
    equity_out, weights_out, traded_out = sim.equity, sim.weights, sim.traded
    open_exposure_out, trades, pnl = sim.open_exposure, sim.trades, sim.pnl

    equity_s = pd.Series(equity_out, index=sessions)
    returns = equity_s / equity_s.shift(1).fillna(1.0) - 1.0
    trades_df = pd.DataFrame(trades, columns=["date", "symbol", "side", "shares", "price", "bps", "notional"])
    first = str(trades_df["date"].iloc[0].date()) if len(trades_df) else None
    notes = []
    if first is None:
        notes.append("no trades: entry rule never fired on a tradable name")
    return EngineResult(
        returns=returns,
        weights=pd.DataFrame(weights_out, index=sessions, columns=symbols),
        traded=pd.Series(traded_out, index=sessions),
        trades=trades_df,
        contributions=pd.Series(pnl, index=symbols).sort_values(ascending=False),
        symbols=symbols,
        missing=missing,
        first_trade_date=first,
        notes=notes,
        open_exposure=pd.Series(open_exposure_out, index=sessions),
    )


def ffill_rows(arr: np.ndarray) -> np.ndarray:
    """Forward-fill NaNs down each column (a name with no bar keeps its last close)."""
    n = arr.shape[0]
    idx = np.where(np.isnan(arr), 0, np.arange(n, dtype=np.int32)[:, None])
    np.maximum.accumulate(idx, axis=0, out=idx)
    return np.take_along_axis(arr, idx, axis=0)  # leading NaNs point at row 0, itself NaN


def intraday_indicator_panels(
    spec: StrategySpec,
    panels: "IntradayPanels",
    rows: int,
    columns: np.ndarray,
    *,
    daily: dict[str, pd.DataFrame] | None = None,
    bench: str = "SPY",
) -> dict[str, np.ndarray]:
    """Each spec indicator per name, as float32 bars x names on the spec's own bars.

    Indicators on the spec's bars use that name's own bars. Slower ones
    (``timeframe: 1h`` / ``1d``) are computed on the slower bars and placed
    on each fast bar only once the slower bar has fully closed
    (``slower_bar_rows``, ``daily_bar_rows``). One name at a time (memory);
    columns not in ``columns`` stay NaN.
    """
    out = {name: np.full((rows, len(panels.symbols)), np.nan, dtype=np.float32) for name in spec.indicators}
    if not spec.indicators:
        return out
    inds = {name: (get_indicator(str(spec.indicators[name]["fn"])), spec.indicator_args(name),
                   spec.indicator_timeframe(name)) for name in spec.indicators}
    by_tf: dict[str | None, list[str]] = {}
    for name, (_, _, tf) in inds.items():
        by_tf.setdefault(tf, []).append(name)
    index = panels.index[:rows]
    bench_close = panels.bench_close[:rows]
    hourly = slower_bar_rows(panels, rows, 60) if "1h" in by_tf else None
    day_rows = daily_bar_rows(panels, rows) if "1d" in by_tf else None
    bench_daily = (daily or {}).get(bench.upper())
    for j in columns:
        has = np.flatnonzero(~np.isnan(panels.close[:rows, j]))
        if len(has):
            frame = pd.DataFrame(
                {
                    "Open": panels.open[has, j].astype(float), "High": panels.high[has, j].astype(float),
                    "Low": panels.low[has, j].astype(float), "Close": panels.close[has, j].astype(float),
                    "Volume": panels.volume[has, j].astype(float), "BenchClose": bench_close[has],
                },
                index=index[has],
            )
            for name in by_tf.get(None, []):
                ind, args, _ = inds[name]
                out[name][has, j] = ind(frame, **args).to_numpy(dtype=float)
            if hourly is not None:
                key, ends = hourly
                bars_1h, last_key = resample_rows(frame, key[has])
                for name in by_tf["1h"]:
                    ind, args, _ = inds[name]
                    out[name][:, j] = place_closed(ind(bars_1h, **args).to_numpy(dtype=float), last_key, ends, rows)
        if day_rows is not None:
            df = (daily or {}).get(panels.symbols[j])
            if df is None or df.empty:
                continue
            frame_d = df[["Open", "High", "Low", "Close", "Volume"]].assign(
                BenchClose=bench_daily["Close"].reindex(df.index) if bench_daily is not None else np.nan
            )
            prior = df.index.searchsorted(panels.sessions, side="left") - 1  # last daily bar before each session
            for name in by_tf["1d"]:
                ind, args, _ = inds[name]
                vals = ind(frame_d, **args).to_numpy(dtype=float)
                per_session = np.where(prior >= 0, vals[np.clip(prior, 0, None)], np.nan)
                out[name][:, j] = per_session[day_rows]
    return out


def slower_bar_rows(panels: "IntradayPanels", rows: int, minutes: int) -> tuple[np.ndarray, np.ndarray]:
    """Which slower bar each fast row belongs to, and the fast row where each slower bar closes.

    Slower bars start at the session's open like every bar here (1h: 09:30,
    10:30 ...). Returns ``key`` (per fast row: session * 10_000 + slot) and
    ``ends`` (sorted keys and, for each, its last scheduled fast row). A slower
    bar has closed at the close of that row, whether or not the name traded in it.
    """
    key = panels.session[:rows].astype(np.int64) * 10_000 + panels.minute[:rows].astype(np.int64) // minutes
    last = np.flatnonzero(np.r_[key[1:] != key[:-1], True])
    return key, np.stack([key[last], last])


def daily_bar_rows(panels: "IntradayPanels", rows: int) -> np.ndarray:
    """Per fast row, its session's position: a daily value is looked up as of the session before.

    A session's own daily bar closes with the closing auction, after its last
    intraday bar, so no intraday bar ever sees it; each session sees the daily
    bars of earlier sessions only.
    """
    return panels.session[:rows]


def resample_rows(frame: pd.DataFrame, key: np.ndarray) -> tuple[pd.DataFrame, np.ndarray]:
    """One name's fast bars grouped into slower bars by ``key`` (sorted); returns the bars and their keys.

    A slower bar is built only from the fast bars inside it, like ``intraday.resample``.
    """
    g = frame.groupby(key, sort=True)
    out = pd.DataFrame({
        "Open": g["Open"].first(), "High": g["High"].max(), "Low": g["Low"].min(),
        "Close": g["Close"].last(), "Volume": g["Volume"].sum(), "BenchClose": g["BenchClose"].last(),
    })
    keys = out.index.to_numpy()
    first = np.r_[True, key[1:] != key[:-1]]
    out.index = frame.index[first]  # each slower bar's first fast bar
    return out, keys


def place_closed(values: np.ndarray, keys: np.ndarray, ends: np.ndarray, rows: int) -> np.ndarray:
    """Slower-bar values on the fast rows: each from the row where its bar closes, carried forward."""
    col = np.full(rows, np.nan)
    at = ends[1][np.searchsorted(ends[0], keys)]
    col[at] = values
    return ffill_rows(col[:, None])[:, 0]


def _bucket_bps(mdv: np.ndarray, buckets) -> np.ndarray:
    out = np.full(mdv.shape, np.nan)
    for bucket in sorted(buckets, key=lambda b: b.min_dollar_volume):
        out[mdv >= bucket.min_dollar_volume] = bucket.bps
    return out


def run_spec_intraday(
    spec: StrategySpec,
    panels: "IntradayPanels",
    bars: dict[str, pd.DataFrame],
    universe: list[str],
    window: ResolvedWindow,
    costs: LabCostSettings,
    *,
    bench: str = "SPY",
) -> EngineResult:
    """Run an intraday spec on ``panels`` (its bar size); ``bars`` are the daily bars (costs).

    Names in ``panels`` but not in ``universe`` are masked out, never copied:
    robustness subsets reuse the full panels. A name needs intraday bars and
    daily bars (for its liquidity bucket); without either it is ``missing``.
    """
    if spec.timeframe != panels.timeframe:
        raise ValueError(f"spec timeframe {spec.timeframe} but panels are {panels.timeframe}")
    wanted = [s.upper() for s in dict.fromkeys(universe)]
    col = {s: j for j, s in enumerate(panels.symbols)}
    no_bars = set(panels.missing)
    have = [s for s in wanted if s in col and s not in no_bars and s in bars and not bars[s].empty]
    missing = [s for s in wanted if s not in have]
    if not have:
        raise ValueError("no intraday bars for any name in the universe")
    symbols = sorted(have)
    cols = np.array([col[s] for s in symbols])
    allowed = np.zeros(len(panels.symbols), dtype=bool)
    allowed[cols] = True

    # Rows through the window's end only: nothing later is visible to anything.
    T = int((panels.index.normalize() <= pd.Timestamp(window.end)).sum())
    calendar = panels.index[:T]
    sessions_mask = window.mask(calendar).to_numpy()
    pos = np.flatnonzero(sessions_mask)
    if not len(pos):
        raise ValueError(f"no bars in {window.name} {window.start}..{window.end}")

    env: dict[str, Any] = {
        "open": panels.open[:T], "high": panels.high[:T], "low": panels.low[:T],
        "close": panels.close[:T], "volume": panels.volume[:T],
        "bar_of_day": panels.bar_of_day[:T, None].astype(float),
        "bars_left_today": panels.bars_left[:T, None].astype(float),
    }
    env.update(intraday_indicator_panels(spec, panels, T, cols, daily=bars, bench=bench))
    env.update(spec.param_values())

    # Liquidity from daily bars, as of the previous session's close.
    day_bars = {s: bars[s] for s in symbols}
    day_bars = {s: df.loc[df.index <= pd.Timestamp(window.end)] for s, df in day_bars.items()}
    dclose = bars_panel(day_bars, "Close").reindex(columns=panels.symbols)
    dvol = bars_panel(day_bars, "Volume").reindex(columns=panels.symbols)
    mdv_day = median_dollar_volume(dclose, dvol, costs.lookback_days).to_numpy(dtype=float)
    prev = dclose.index.searchsorted(panels.sessions, side="left") - 1
    mdv_sess = np.where((prev >= 0)[:, None], mdv_day[np.clip(prev, 0, None)], np.nan)
    sess_rows = panels.session[:T]
    mdv = mdv_sess[sess_rows].astype(np.float32)
    ic = costs.intraday
    # A fill in the first minutes after the open (not the opening print) pays more.
    fill_minute = np.append(panels.minute[1:T], -1).astype(int)
    surcharge = np.where((fill_minute > 0) & (fill_minute < ic.open_window_minutes), ic.open_window_multiplier, 1.0)
    bps = (_bucket_bps(mdv_sess, ic.buckets)[sess_rows] * surcharge[:, None]).astype(np.float32)
    worst_bps = max(b.bps for b in ic.buckets) * surcharge

    has_bar = ~np.isnan(env["close"])
    mark = ffill_rows(panels.close[:T])
    rules = spec.rules
    entry = Evaluator(rules["entry"], env).full() & has_bar & ~np.isnan(bps) & allowed
    shape = env["close"].shape
    rank = np.broadcast_to(Evaluator(rules["rank"], env).values(), shape) if "rank" in rules else mdv
    exit_eval = Evaluator(rules["exit"], env)
    exit_full = None if rules["exit"].uses_state else np.broadcast_to(exit_eval.full(), shape)

    bars_left = panels.bars_left[pos]
    session_end = bars_left == 0
    sim = _simulate(
        spec, calendar, pos, list(panels.symbols), panels.open[:T], mark, bps, worst_bps,
        entry, rank, exit_eval, exit_full, weight_rows=session_end, carry_exits=True,
    )
    days = pd.DatetimeIndex(panels.sessions[sess_rows[pos]])
    end_k = np.flatnonzero(session_end)
    start_k = np.flatnonzero(panels.bar_of_day[pos] == 0)
    sessions = days[end_k]
    if len(start_k) != len(end_k):
        raise ValueError("window does not cover whole sessions")
    equity_s = pd.Series(sim.equity[end_k], index=sessions)
    returns = equity_s / equity_s.shift(1).fillna(1.0) - 1.0
    prior = equity_s.shift(1).fillna(1.0)
    overnight = pd.Series(sim.pre_open_equity[start_k], index=sessions) / prior - 1.0
    intraday = (1.0 + returns) / (1.0 + overnight) - 1.0

    trades_df = pd.DataFrame(sim.trades, columns=["date", "symbol", "side", "shares", "price", "bps", "notional"])
    first = str(trades_df["date"].iloc[0].date()) if len(trades_df) else None
    notes = [] if first else ["no trades: entry rule never fired on a tradable name"]
    pnl = pd.Series(sim.pnl, index=panels.symbols).reindex(symbols)
    return EngineResult(
        returns=returns,
        weights=pd.DataFrame(sim.weights[:, cols], index=sessions, columns=symbols),
        traded=pd.Series(sim.traded, index=days).groupby(level=0).sum().reindex(sessions),
        trades=trades_df,
        contributions=pnl.sort_values(ascending=False),
        symbols=symbols,
        missing=missing,
        first_trade_date=first,
        notes=notes,
        open_exposure=pd.Series(sim.open_exposure[start_k], index=sessions),
        bar_exposure=pd.DataFrame(
            {"open": sim.open_exposure, "close": sim.close_exposure}, index=calendar[pos]
        ),
        overnight=overnight,
        intraday=intraday,
    )
