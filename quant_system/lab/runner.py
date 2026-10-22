"""Score one spec on one window: lock checks, engine, benchmarks, metrics, trial log."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np
import pandas as pd

from quant_system.config.loader import AppConfig
from quant_system.lab.benchmarks import benchmark_returns
from quant_system.lab.data import load_lab_bars
from quant_system.lab.engine import EngineResult, ffill_rows, run_spec, run_spec_intraday
from quant_system.lab.intraday import IntradayPanels, IntradaySource
from quant_system.lab.metrics import compare, deflated_sharpe, pass_bar, summary
from quant_system.lab.settings import LabSettings, load_lab_settings
from quant_system.lab.spec import StrategySpec, require_spec_timeframe
from quant_system.lab.trials import (
    SCHEMA_VERSION,
    git_state,
    record_trial,
    smoke_dir,
    stamped_name,
    trial_stats,
    write_json_atomic,
)
from quant_system.lab.universe import universe_symbols
from quant_system.lab.windows import ResolvedWindow, authorize_run, get_window

TOP_CONTRIBUTORS = 5


@dataclass
class Evaluation:
    """One engine run with its benchmarks and metrics (nothing logged)."""

    result: EngineResult
    benchmarks: dict[str, dict[str, Any]]
    strategy: dict[str, Any]  # metrics.summary of the strategy
    comparison: dict[str, Any]  # metrics.compare vs the benchmarks
    # Intraday specs: the equal-weight basket's value at each bar's open and close.
    ew_bars: pd.DataFrame | None = None

    def metrics(self) -> dict[str, Any]:
        return {
            **self.strategy,
            "first_trade_date": self.result.first_trade_date,
            "beats_benchmarks": self.comparison["beats_benchmarks"],
            "vs_benchmarks": self.comparison["vs_benchmarks"],
        }


def equal_weight_bars(panels: IntradayPanels, ew: dict[str, Any], index: pd.DatetimeIndex) -> pd.DataFrame:
    """The equal-weight benchmark's value at each bar's open and close (intraday specs).

    Cash (1.0) until its entry; on the entry session's first bar it is bought at
    the open (value 1.0 there), then held at the daily benchmark's share counts.
    A name with no bar keeps its last close.
    """
    out = pd.DataFrame({"open": 1.0, "close": 1.0}, index=index)
    qty = ew.get("qty")
    if qty is None or qty.empty or not ew.get("entry_date"):
        return out
    col = {s: j for j, s in enumerate(panels.symbols)}
    names = [s for s in qty.index if s in col]
    rows = panels.index.get_indexer(index)
    cols = np.array([col[s] for s in names])
    q = qty[names].to_numpy(dtype=float)
    upto = int(rows.max()) + 1
    close = panels.close[:upto][:, cols]
    mark = ffill_rows(close)[rows].astype(float)
    open_ = panels.open[:upto][:, cols][rows].astype(float)
    open_ = np.where(np.isnan(open_), mark, open_)
    v_close = np.nan_to_num(mark) @ q
    v_open = np.nan_to_num(open_) @ q
    entry = pd.Timestamp(ew["entry_date"])
    day = index.normalize()
    held = day >= entry
    first = held & ~np.r_[False, held[:-1]]
    out.loc[held, "close"] = v_close[held]
    out.loc[held & ~first, "open"] = v_open[held & ~first]
    return out


def evaluate(
    spec: StrategySpec,
    bars: dict[str, pd.DataFrame],
    universe: list[str],
    window: ResolvedWindow,
    settings: LabSettings,
    *,
    intraday: IntradaySource | None = None,
) -> Evaluation:
    """Engine + SPY / equal-weight benchmarks + metrics, over ``universe``.

    Intraday specs read their bars from ``intraday``; ``bars`` (daily) still
    price their liquidity and the benchmarks, which are the same daily
    buy-and-holds as for daily specs.
    """
    panels = None
    if spec.timeframe:
        if intraday is None:
            raise ValueError(f"spec {spec.id} runs on {spec.timeframe} bars; pass an IntradaySource")
        panels = intraday.panels(spec.timeframe, universe, bench=settings.benchmark, daily=bars)
        result = run_spec_intraday(spec, panels, bars, universe, window, settings.costs, bench=settings.benchmark)
    else:
        result = run_spec(spec, bars, universe, window, settings.costs, bench=settings.benchmark)
    bench = benchmark_returns(bars, result.symbols, window, settings.costs, spy=settings.benchmark)
    strat = summary(
        result.returns, weights=result.weights, trade_count=result.trade_count, traded=result.traded
    )
    ew_bars = None
    if panels is not None:
        strat["timeframe"] = spec.timeframe
        # Where the return came from: previous close to the first bar's open, then the session.
        strat["overnight_return"] = float((1.0 + result.overnight).prod() - 1.0)
        strat["intraday_return"] = float((1.0 + result.intraday).prod() - 1.0)
        # Name-sessions (data.start..ceiling) of the scored names left out as suspect bars.
        strat["skipped_sessions"] = sum(panels.skipped.get(s, 0) for s in result.symbols)
        ew_bars = equal_weight_bars(panels, bench["equal_weight"], result.bar_exposure.index)
    cmp_ = compare(result.returns, {k: v["returns"] for k, v in bench.items()})
    return Evaluation(result, bench, strat, cmp_, ew_bars)


def score_spec(spec: StrategySpec, **kwargs: Any) -> dict[str, Any]:
    """Run, log and score ``spec``; returns the scorecard (see ``score_spec_full``)."""
    return score_spec_full(spec, **kwargs)[0]


def score_spec_full(
    spec: StrategySpec,
    *,
    window: str = "discovery",
    author_bot: str | None = None,
    frozen_trials: dict[str, Any] | None = None,
    settings: LabSettings | None = None,
    cfg: AppConfig | None = None,
    bars: dict[str, pd.DataFrame] | None = None,
    data_dir: Path | str | None = None,
    today: date | None = None,
    extra: Callable[[Evaluation, ResolvedWindow], Mapping[str, Any]] | None = None,
    intraday: IntradaySource | None = None,
) -> tuple[dict[str, Any], Evaluation]:
    """Run, log and score ``spec``; returns the scorecard and the evaluation behind it.

    Every run is written to the trial log; there is no way to score without
    counting. Validation and holdout need ``frozen_trials`` (the discovery trial
    count and Sharpe variance frozen at nomination), and their record is written
    with the verdict before anything is returned. ``extra(evaluation, window)``
    adds fields to both the record and the scorecard, computed before the record
    is written.
    """
    s = settings or load_lab_settings()
    require_spec_timeframe(spec)  # 30m / 2h are robustness variants, never trials
    if get_window(window).locked and frozen_trials is None:
        raise ValueError(f"{window} needs frozen discovery trial stats for the deflated Sharpe")
    rw = authorize_run(window, spec.hash, settings=s, today=today, data_dir=data_dir)
    universe = universe_symbols(spec.universe, settings=s, cfg=cfg)
    if bars is None:
        bars = load_lab_bars([s.benchmark, *universe], settings=s, data_dir=data_dir, today=today)
    if spec.timeframe and intraday is None:
        intraday = IntradaySource(settings=s, data_dir=data_dir, today=today)
    ev = evaluate(spec, bars, universe, rw, s, intraday=intraday)
    added = dict(extra(ev, rw)) if extra else {}
    result, bench, strat, cmp_ = ev.result, ev.benchmarks, ev.strategy, ev.comparison
    required = (s.benchmark.upper(), "equal_weight")
    metrics: dict[str, Any] = ev.metrics()

    def _verdict(stats: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        dsr = deflated_sharpe(result.returns, int(stats["n_trials"]), float(stats["sharpe_variance"]))
        return dsr, pass_bar(rw.name, cmp_, dsr, required=required)

    author = author_bot or spec.author_bot or "unknown"
    start = pd.Timestamp(s.data.start)
    universe_info = {
        "name": spec.universe,
        "names": len(result.symbols),
        "missing": result.missing,
        # Names with warm-up bars from before data.start (lab.data); the rest start late.
        "warmup_names": sum(1 for sym in result.symbols if bars[sym].index.min() < start),
    }
    if frozen_trials is not None:
        # Locked windows: the verdict goes into the record, and the record is written first.
        dsr, verdict = _verdict(frozen_trials)
        metrics.update(deflated_sharpe=dsr, passed=verdict["passed"])
    path = record_trial(
        spec=spec.canonical(),
        window=rw.name,
        metrics=metrics,
        returns=result.returns,
        author_bot=author,
        extra={"universe": universe_info, **added},
        data_dir=data_dir,
    )
    stats = frozen_trials or trial_stats("discovery", data_dir=data_dir)
    if frozen_trials is None:
        # Discovery: the deflated Sharpe moves as trials accrue, so it is shown, not stored.
        dsr, verdict = _verdict(stats)

    contrib = result.contributions
    total = float(contrib.sum())
    top = contrib.head(TOP_CONTRIBUTORS)
    card = {
        "spec_id": spec.id,
        "spec_hash": spec.hash,
        "author_bot": author,
        "window": {"name": rw.name, "start": str(rw.start), "end": str(rw.end)},
        "universe": universe_info,
        "strategy": {**strat, "first_trade_date": result.first_trade_date},
        "benchmarks": {
            k: {
                **summary(v["returns"]),
                "entry_date": v["entry_date"],
                "names": len(v["tickers"]),
                "excluded": len(v["excluded"]),
            }
            for k, v in bench.items()
        },
        "vs_benchmarks": cmp_["vs_benchmarks"],
        "deflated_sharpe": dsr,
        "trials": {"n_trials": stats["n_trials"], "sharpe_variance": stats["sharpe_variance"]},
        "verdict": verdict,
        "top_contributors": [{"symbol": sym, "pnl": float(v)} for sym, v in top.items()],
        "top_contributor_share": float(top.iloc[0] / total) if total > 0 and len(top) else None,
        "notes": result.notes,
        **added,
        "trial_file": str(path),
    }
    return card, ev


def smoke_spec(
    spec: StrategySpec,
    *,
    author_bot: str | None = None,
    settings: LabSettings | None = None,
    cfg: AppConfig | None = None,
    bars: dict[str, pd.DataFrame] | None = None,
    data_dir: Path | str | None = None,
    today: date | None = None,
    intraday: IntradaySource | None = None,
) -> dict[str, Any]:
    """Run ``spec`` end to end on discovery as a test of the pipeline, not of the idea.

    Nothing reaches the trial log, so the run never counts as a trial, and so
    nothing about how it did comes back: no returns, Sharpe, benchmarks,
    verdict or contributors. Only that it ran, how much it traded and how long
    it took. A record of the run goes to ``smoke_dir()`` so test runs stay on file.
    """
    started = time.perf_counter()
    s = settings or load_lab_settings()
    require_spec_timeframe(spec)
    rw = authorize_run("discovery", spec.hash, settings=s, today=today, data_dir=data_dir)
    universe = universe_symbols(spec.universe, settings=s, cfg=cfg)
    if bars is None:
        bars = load_lab_bars([s.benchmark, *universe], settings=s, data_dir=data_dir, today=today)
    if spec.timeframe and intraday is None:
        intraday = IntradaySource(settings=s, data_dir=data_dir, today=today)
    ev = evaluate(spec, bars, universe, rw, s, intraday=intraday)
    result = ev.result
    author = author_bot or spec.author_bot or "unknown"
    commit, dirty = git_state()
    card: dict[str, Any] = {
        "kind": "smoke",
        "spec_id": spec.id,
        "spec_hash": spec.hash,
        "author_bot": author,
        "timeframe": spec.timeframe or "1d",
        "window": {"name": rw.name, "start": str(rw.start), "end": str(rw.end)},
        "universe": {"name": spec.universe, "names": len(result.symbols), "missing": len(result.missing)},
        "sessions": int(len(result.returns)),
        "trade_count": int(result.trade_count),
        "first_trade_date": result.first_trade_date,
        "skipped_sessions": ev.strategy.get("skipped_sessions"),
        "notes": result.notes,
        "seconds": round(time.perf_counter() - started, 1),
    }
    stamp = datetime.now(timezone.utc)
    record = {"schema": SCHEMA_VERSION, "recorded_at": stamp.isoformat(timespec="microseconds"),
              "git_commit": commit, "git_dirty": dirty, **card, "spec": spec.canonical()}
    path = write_json_atomic(smoke_dir(data_dir) / stamped_name(author, spec.hash, stamp), record)
    card["smoke_file"] = str(path)
    return card
