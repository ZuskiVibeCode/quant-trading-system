"""`lab rescore`: score every logged discovery spec again after a scoring change, adding no trials.

A trial is a distinct spec hash, and the latest record per hash is the one that
counts (``trials.trial_stats``), so rerunning each logged spec replaces its score
and leaves the trial count alone. Each rerun is an ordinary trial record under
the spec's own author, marked ``rescore: true`` so the lab journal doesn't show
it as that bot's work for the day. Robustness variants are not rerun; nomination
reruns the robustness checks itself.

A logged spec that no longer loads, or now hashes differently (the spec format
changed since it ran), is skipped and listed: rerunning it would add a trial.
Runs from a clean checkout only, like nomination, since it rewrites every
spec's current score.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from quant_system.config.loader import AppConfig
from quant_system.lab.data import load_lab_bars, read_warmup_manifest
from quant_system.lab.nominate import NominationError, require_clean_checkout
from quant_system.lab.runner import score_spec
from quant_system.lab.settings import LabSettings, load_lab_settings
from quant_system.lab.spec import StrategySpec
from quant_system.lab.trials import load_trials, trial_stats
from quant_system.lab.universe import universe_symbols


class RescoreError(NominationError):
    """A rescore that can't go ahead (dirty checkout, no warm-up bars)."""


def _passed(metrics: dict[str, Any]) -> bool | None:
    v = metrics.get("beats_benchmarks")
    return bool(v) if v is not None else None


def rescore_discovery(
    *,
    settings: LabSettings | None = None,
    cfg: AppConfig | None = None,
    bars: dict[str, pd.DataFrame] | None = None,
    data_dir: Path | str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Rerun the latest discovery record of every spec hash; the trial count must not move."""
    s = settings or load_lab_settings()
    try:
        commit = require_clean_checkout()
    except NominationError as exc:
        raise RescoreError(str(exc)) from None
    if bars is None and not (read_warmup_manifest(data_dir).get("tickers") or {}):
        raise RescoreError("no warm-up bars cached; run `lab data warmup` first")

    before = trial_stats("discovery", data_dir=data_dir)
    latest: dict[str, dict[str, Any]] = {}
    for rec in load_trials(window="discovery", kind="trial", data_dir=data_dir):
        latest[rec["spec_hash"]] = rec  # oldest first, so the last one wins

    loaded: dict[str, dict[str, pd.DataFrame]] = {}
    rescored: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for h, rec in latest.items():
        row = {"spec_hash": h, "spec_id": rec.get("spec_id"), "author_bot": rec.get("author_bot")}
        try:
            spec = StrategySpec.model_validate(rec.get("spec") or {})
        except ValueError as exc:
            skipped.append({**row, "reason": f"spec no longer loads: {str(exc)[:300]}"})
            continue
        if spec.hash != h:
            skipped.append({**row, "reason": f"now hashes to {spec.hash}; rerunning would add a trial"})
            continue
        use = bars
        if use is None:
            key = spec.universe.strip().lower()
            if key not in loaded:
                names = universe_symbols(spec.universe, settings=s, cfg=cfg)
                loaded[key] = load_lab_bars([s.benchmark, *names], settings=s, data_dir=data_dir, today=today)
            use = loaded[key]
        card = score_spec(
            spec, window="discovery", author_bot=rec.get("author_bot") or "unknown", settings=s, cfg=cfg,
            bars=use, data_dir=data_dir, today=today, extra=lambda ev, rw: {"rescore": True},
        )
        old = rec.get("metrics") or {}
        rescored.append({
            **row,
            "old_sharpe": old.get("sharpe"),
            "sharpe": card["strategy"]["sharpe"],
            "old_beats_benchmarks": _passed(old),
            "beats_benchmarks": bool(card["verdict"]["passed"]),
            "warmup_names": card["universe"]["warmup_names"],
            "names": card["universe"]["names"],
        })

    after = trial_stats("discovery", data_dir=data_dir)
    if after["n_trials"] != before["n_trials"]:  # can't happen: every rerun reuses a logged hash
        raise RescoreError(f"trial count moved {before['n_trials']} -> {after['n_trials']}; check the trial log")
    return {
        "git_commit": commit,
        "n_trials": after["n_trials"],
        "rescored": len(rescored),
        "skipped": skipped,
        "now_beat_benchmarks": sum(r["beats_benchmarks"] for r in rescored),
        "used_to_beat_benchmarks": sum(bool(r["old_beats_benchmarks"]) for r in rescored),
        "specs": rescored,
    }
