"""Score one spec on one window: lock checks, engine, benchmarks, metrics, trial log."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from quant_system.config.loader import AppConfig
from quant_system.lab.benchmarks import benchmark_returns
from quant_system.lab.data import load_lab_bars
from quant_system.lab.engine import run_spec
from quant_system.lab.metrics import compare, deflated_sharpe, pass_bar, summary
from quant_system.lab.settings import LabSettings, load_lab_settings
from quant_system.lab.spec import StrategySpec
from quant_system.lab.trials import record_trial, trial_stats
from quant_system.lab.universe import universe_symbols
from quant_system.lab.windows import authorize_run, get_window

TOP_CONTRIBUTORS = 5


def score_spec(
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
) -> dict[str, Any]:
    """Run, log and score ``spec``; returns the scorecard.

    Every run is written to the trial log; there is no way to score without
    counting. Validation and holdout need ``frozen_trials`` (the discovery trial
    count and Sharpe variance frozen at nomination), and their record is written
    with the verdict before anything is returned.
    """
    s = settings or load_lab_settings()
    if get_window(window).locked and frozen_trials is None:
        raise ValueError(f"{window} needs frozen discovery trial stats for the deflated Sharpe")
    rw = authorize_run(window, spec.hash, settings=s, today=today, data_dir=data_dir)
    universe = universe_symbols(spec.universe, settings=s, cfg=cfg)
    if bars is None:
        bars = load_lab_bars([s.benchmark, *universe], settings=s, data_dir=data_dir, today=today)
    result = run_spec(spec, bars, universe, rw, s.costs, bench=s.benchmark)
    bench = benchmark_returns(bars, result.symbols, rw, s.costs, spy=s.benchmark)

    strat = summary(result.returns, weights=result.weights, trade_count=result.trade_count, traded=result.traded)
    cmp_ = compare(result.returns, {k: v["returns"] for k, v in bench.items()})
    required = (s.benchmark.upper(), "equal_weight")
    metrics: dict[str, Any] = {
        **strat,
        "first_trade_date": result.first_trade_date,
        "beats_benchmarks": cmp_["beats_benchmarks"],
        "vs_benchmarks": cmp_["vs_benchmarks"],
    }

    def _verdict(stats: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        dsr = deflated_sharpe(result.returns, int(stats["n_trials"]), float(stats["sharpe_variance"]))
        return dsr, pass_bar(rw.name, cmp_, dsr, required=required)

    author = author_bot or spec.author_bot or "unknown"
    universe_info = {"name": spec.universe, "names": len(result.symbols), "missing": result.missing}
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
        extra={"universe": universe_info},
        data_dir=data_dir,
    )
    stats = frozen_trials or trial_stats("discovery", data_dir=data_dir)
    if frozen_trials is None:
        # Discovery: the deflated Sharpe moves as trials accrue, so it is shown, not stored.
        dsr, verdict = _verdict(stats)

    contrib = result.contributions
    total = float(contrib.sum())
    top = contrib.head(TOP_CONTRIBUTORS)
    return {
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
        "trial_file": str(path),
    }
