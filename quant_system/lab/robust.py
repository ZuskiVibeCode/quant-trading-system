"""Gauntlet step 2 (`lab robust`): does a spec hold up beyond its exact settings?

On the discovery window only. The spec itself is scored and logged as a normal
trial; every variant is logged as ``kind: robustness`` under it, so variants
never add to the trial count. Checks (thresholds in ``config/lab.yaml`` robust):

- nudges: each setting moved -20/-10/+10/+20% (within the spec's own min/max),
  one at a time. Enough variants must still beat SPY and equal-weight, and
  their median Sharpe must stay near the spec's: one sharp peak fails.
- sectors / cap buckets: rerun inside each sector and each cap bucket of the
  broad universe; the strategy's Sharpe must beat that subset's equal-weight
  Sharpe in enough of them.
- selloffs: from the spec's own run, the worst drawdown inside Q4 2018 and
  Feb-Apr 2020 may not be much worse than SPY's or equal-weight's.
- single stock: rerun without the top contributing name; it must still pass.

A check with nothing to test (no settings, a universe without sector data, no
profitable name) is ``skipped``: it doesn't fail the spec, but the report is
then ``complete: false``.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from quant_system.config.loader import AppConfig
from quant_system.lab.data import load_lab_bars
from quant_system.lab.metrics import max_drawdown, pass_bar
from quant_system.lab.runner import Evaluation, evaluate, score_spec_full
from quant_system.lab.settings import LabRobustSettings, LabSettings, Selloff, lab_data_dir, load_lab_settings
from quant_system.lab.spec import StrategySpec
from quant_system.lab.trials import git_state, record_trial, stamped_name, write_json_atomic
from quant_system.lab.universe import load_broad_universe, universe_symbols
from quant_system.lab.windows import ResolvedWindow, authorize_run


def robust_dir(data_dir: Path | str | None = None) -> Path:
    return (Path(data_dir) if data_dir else lab_data_dir()) / "robust"


def nudged_values(spec: StrategySpec, nudges: list[float]) -> Iterator[tuple[str, float, float]]:
    """(setting, change, value) for each nudge, clipped to the setting's range, deduplicated."""
    for name, p in spec.params.items():
        seen = {round(p.value, 10)}
        for change in nudges:
            value = round(min(max(p.value * (1.0 + change), p.min), p.max), 10)
            if value in seen:
                continue
            seen.add(value)
            yield name, change, value


def _groups(spec: StrategySpec, settings: LabSettings, symbols: list[str], key: str) -> dict[str, list[str]]:
    """Symbols by sector or cap bucket; empty for universes without that metadata."""
    if spec.universe.strip().lower() not in ("broad", "all"):
        return {}
    have = set(symbols)
    out: dict[str, list[str]] = defaultdict(list)
    for row in load_broad_universe(settings):
        sym = str(row["symbol"]).upper()
        if sym in have and row.get(key):
            out[str(row[key])].append(sym)
    return dict(sorted(out.items()))


def subset_row(ev: Evaluation) -> dict[str, Any]:
    vs = ev.comparison["vs_benchmarks"]
    return {
        "sharpe": ev.strategy["sharpe"],
        "cagr": ev.strategy["cagr"],
        "trade_count": ev.strategy.get("trade_count"),
        "ew_sharpe": vs["equal_weight"]["sharpe"],
        "beats_equal_weight_sharpe": vs["equal_weight"]["sharpe_diff"] > 0,
    }


def selloff_rows(ev: Evaluation, selloffs: list[Selloff], dd_ratio: float) -> list[dict[str, Any]]:
    """Return and worst drawdown inside each selloff vs the benchmarks' (periods outside the run skipped).

    ``ok``: the strategy's drawdown is at most ``dd_ratio`` x the worse benchmark's.
    """
    rows = []
    for sell in selloffs:
        lo, hi = pd.Timestamp(sell.start), pd.Timestamp(sell.end)
        part = ev.result.returns.loc[lo:hi]
        if part.empty:
            continue
        dds = {k: max_drawdown(v["returns"].loc[lo:hi]) for k, v in ev.benchmarks.items()}
        worst = max(dds.values())
        dd = max_drawdown(part)
        rows.append({"name": sell.name, "start": str(sell.start), "end": str(sell.end),
                     "return": float((1 + part).prod() - 1), "max_drawdown": dd,
                     "benchmark_drawdowns": dds, "ok": dd <= dd_ratio * worst + 1e-12})
    return rows


def _status(ok: bool) -> str:
    return "pass" if ok else "fail"


def nudge_check(rows: list[dict[str, Any]], base_sharpe: float, r: LabRobustSettings) -> dict[str, Any]:
    """Enough nudged variants still pass, and their median Sharpe isn't far below the spec's."""
    if not rows:
        return {"status": "skipped", "reason": "no tunable settings"}
    share = sum(bool(x["passes"]) for x in rows) / len(rows)
    median = statistics.median(float(x["sharpe"]) for x in rows)
    ok = share >= r.nudge_pass_share and base_sharpe > 0 and median >= r.peak_ratio * base_sharpe
    return {"status": _status(ok), "pass_share": share, "median_sharpe": median,
            "base_sharpe": base_sharpe, "variants": rows}


def run_robust(
    spec: StrategySpec,
    *,
    author_bot: str | None = None,
    settings: LabSettings | None = None,
    cfg: AppConfig | None = None,
    bars: dict[str, pd.DataFrame] | None = None,
    data_dir: Path | str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    s = settings or load_lab_settings()
    r = s.robust
    author = author_bot or spec.author_bot or "unknown"
    rw: ResolvedWindow = authorize_run(
        "discovery", spec.hash, kind="robustness", settings=s, today=today, data_dir=data_dir
    )
    universe = universe_symbols(spec.universe, settings=s, cfg=cfg)
    if bars is None:
        bars = load_lab_bars([s.benchmark, *universe], settings=s, data_dir=data_dir, today=today)
    required = (s.benchmark.upper(), "equal_weight")

    base_card, base = score_spec_full(
        spec, window="discovery", author_bot=author, settings=s, cfg=cfg, bars=bars,
        data_dir=data_dir, today=today,
    )
    base_sharpe = float(base.strategy["sharpe"])

    def passes(ev: Evaluation) -> bool:
        return pass_bar("discovery", ev.comparison, required=required)["passed"]

    def run(variant: StrategySpec, names: list[str], check: str, detail: dict[str, Any]) -> Evaluation:
        ev = evaluate(variant, bars, names, rw, s)
        record_trial(
            spec=variant.canonical(), window="discovery", metrics=ev.metrics(), returns=ev.result.returns,
            author_bot=author, kind="robustness", parent_hash=spec.hash,
            extra={"check": check, "detail": detail}, data_dir=data_dir,
        )
        return ev

    checks: dict[str, dict[str, Any]] = {}

    # 1. Nudged settings.
    rows = []
    for name, change, value in nudged_values(spec, r.nudges):
        ev = run(spec.with_params({name: value}), universe, "nudge", {"param": name, "change": change, "value": value})
        rows.append({"param": name, "change": change, "value": value, "sharpe": ev.strategy["sharpe"],
                     "cagr": ev.strategy["cagr"], "passes": passes(ev)})
    checks["nudges"] = nudge_check(rows, base_sharpe, r)

    # 2-3. Sectors and cap buckets.
    for check, key, need in (("sectors", "sector", r.sector_pass_share), ("cap_buckets", "cap_bucket", r.cap_pass_share)):
        groups = _groups(spec, s, base.result.symbols, key)
        scored, too_small = [], []
        for group, names in groups.items():
            if len(names) < r.min_subset_names:
                too_small.append(group)
                continue
            ev = run(spec, names, check, {key: group, "names": len(names)})
            scored.append({key: group, "names": len(names), **subset_row(ev)})
        if scored:
            share = sum(x["beats_equal_weight_sharpe"] for x in scored) / len(scored)
            checks[check] = {"status": _status(share >= need), "pass_share": share, "groups": scored,
                             "skipped_small": too_small}
        else:
            reason = "universe has no sector / cap data" if not groups else "every group too small"
            checks[check] = {"status": "skipped", "reason": reason}

    # 4. Selloffs, from the spec's own run.
    rows = selloff_rows(base, r.selloffs, r.selloff_dd_ratio)
    checks["selloffs"] = (
        {"status": _status(all(x["ok"] for x in rows)), "periods": rows}
        if rows else {"status": "skipped", "reason": "no selloff inside the window"}
    )

    # 5. Without the top contributor.
    contrib = base.result.contributions
    if len(contrib) and contrib.iloc[0] > 0 and len(base.result.symbols) > 1:
        top = str(contrib.index[0])
        names = [x for x in base.result.symbols if x != top]
        ev = run(spec, names, "single_stock", {"removed": top})
        total = float(contrib.sum())
        checks["single_stock"] = {
            "status": _status(passes(ev)), "removed": top,
            "top_share": float(contrib.iloc[0] / total) if total > 0 else None,
            "sharpe": ev.strategy["sharpe"], "cagr": ev.strategy["cagr"], "passes": passes(ev),
        }
    else:
        checks["single_stock"] = {"status": "skipped", "reason": "no profitable name to remove"}

    base_passed = bool(base_card["verdict"]["passed"])
    statuses = [c["status"] for c in checks.values()]
    report = {
        "spec_id": spec.id,
        "spec_hash": spec.hash,
        "author_bot": author,
        "window": base_card["window"],
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": git_state()[0],
        "git_dirty": git_state()[1],
        "passed": base_passed and "fail" not in statuses,
        "complete": "skipped" not in statuses,
        "base": {"passed": base_passed, "strategy": base_card["strategy"],
                 "vs_benchmarks": base_card["vs_benchmarks"], "trial_file": base_card["trial_file"]},
        "checks": checks,
        "thresholds": r.model_dump(mode="json"),
    }
    path = write_json_atomic(robust_dir(data_dir) / stamped_name(author, spec.hash), report)
    report["report_file"] = str(path)
    return report
