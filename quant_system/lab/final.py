"""Validation and holdout (`lab validate`, `lab holdout`): the single locked runs.

These run only on the validation machine (Sabastian's, never Grok's), from a
clean checkout, with ``QUANT_LAB_UNLOCK`` naming the window and the data
ceiling raised. Input is a nomination packet (``lab.nominate``).

Validation, in order:

0. Machine guard: refuses unless the data ceiling is past 2021-12-31 and
   ``QUANT_LAB_UNLOCK`` names the window, then a clean checkout.
1. Window rules (unlock, one run per spec hash), before any work.
2. The frozen trial count must not go backwards: a packet nominated later than
   one already validated may not carry fewer discovery trials (the shared trial
   log on Grok's machine lost records, or someone removed some).
3. The discovery gate is recomputed here, on this machine's data: the full
   robustness run must pass with a complete report, and the spec's own verdict
   and ``git_dirty`` are read back from the trial log. Nothing a bot wrote on the
   shared machine is trusted except the frozen trial stats. A spec that fails
   here never gets a validation score.
4. One run on 2022-2024 with the frozen stats. The verdict, the recheck and the
   breakouts (2022 selloff, each theme) are in the trial record, written before
   anything is returned or printed.

Holdout runs only for a spec whose validation record passed, with the frozen
stats stored in that record. The breakouts are reported, not part of the pass
bar. Validation and holdout records stay in this machine's trial log; never
copy them to Grok's machine.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from quant_system.config.loader import AppConfig, load_config
from quant_system.lab.data import load_lab_bars
from quant_system.lab.intraday import IntradaySource
from quant_system.lab.nominate import (
    NominationError,
    check_frozen_trials,
    gate_failures,
    nominated_at,
    recheck_summary,
    require_clean_checkout,
)
from quant_system.lab.robust import run_robust, selloff_rows, subset_row
from quant_system.lab.runner import Evaluation, evaluate, score_spec_full
from quant_system.lab.settings import LabSettings, data_ceiling, load_lab_settings
from quant_system.lab.spec import StrategySpec
from quant_system.lab.trials import load_trials
from quant_system.lab.universe import theme_symbols, universe_symbols
from quant_system.lab.windows import UNLOCK_ENV, WINDOWS, ResolvedWindow, WindowLocked, authorize_run, unlocked_windows

BREAKOUT_NOTE = "reported only; not part of the pass bar"


def require_validation_machine(window: str, settings: LabSettings, *, today: date | None = None) -> None:
    """Refuse unless this looks like the validation machine: data past discovery and the window unlocked.

    Grok's machine keeps the ceiling at the end of discovery and never sets the
    unlock, so validation and holdout can't run there by accident.
    """
    ceiling = data_ceiling(settings, today=today)
    discovery_end = WINDOWS["discovery"].end
    if ceiling <= discovery_end:
        raise WindowLocked(
            f"data ceiling is {ceiling} (not past {discovery_end}): {window} runs only on the "
            "validation machine, never on Grok's"
        )
    if window not in unlocked_windows():
        raise WindowLocked(f"{window} is locked; set {UNLOCK_ENV}={window} on the validation machine")


def check_trial_count_order(packet: dict[str, Any], *, data_dir: Path | str | None = None) -> None:
    """Refuse a packet whose frozen trial count disagrees with nomination order."""
    mine_t, mine_n = nominated_at(packet), packet["frozen_trials"]["n_trials"]
    for rec in load_trials(window="validation", kind="trial", data_dir=data_dir):
        nom, frozen = rec.get("nomination") or {}, rec.get("frozen_trials") or {}
        n = frozen.get("n_trials")
        try:
            t = nominated_at(nom)
        except (KeyError, TypeError, ValueError):
            continue
        if not isinstance(n, int):
            continue
        if (t <= mine_t and n > mine_n) or (t >= mine_t and n < mine_n):
            raise NominationError(
                f"frozen trial count went backwards: {nom.get('spec_hash')} nominated {t.isoformat()} "
                f"with {n} trials, this packet {mine_t.isoformat()} with {mine_n}. The shared trial "
                "log may have lost records; check it before validating anything else"
            )


def validated_stats(spec_hash: str, *, data_dir: Path | str | None = None) -> dict[str, Any]:
    """Frozen stats from this spec's passed validation record; holdout is for survivors only."""
    recs = load_trials(window="validation", kind="trial", spec_hash=spec_hash, data_dir=data_dir)
    if not recs:
        raise NominationError(f"spec {spec_hash} has no validation run on this machine; holdout comes after")
    rec = recs[-1]
    if (rec.get("metrics") or {}).get("passed") is not True:
        raise NominationError(f"spec {spec_hash} did not pass validation; holdout is for survivors only")
    return check_frozen_trials(rec.get("frozen_trials"), Path(rec["path"]).name)


def breakouts(
    spec: StrategySpec,
    ev: Evaluation,
    rw: ResolvedWindow,
    bars: dict[str, pd.DataFrame],
    settings: LabSettings,
    cfg: AppConfig,
    intraday: IntradaySource | None = None,
) -> dict[str, Any]:
    """Selloffs inside the window and the spec rerun on each theme's names (and all of them)."""
    inside = [x for x in settings.locked.selloffs if rw.start <= x.start and x.end <= rw.end]
    themes: dict[str, Any] = {}
    for name in ["themes", *cfg.universe.themes]:
        names = theme_symbols(cfg, None if name == "themes" else name)
        t = evaluate(spec, bars, names, rw, settings, intraday=intraday)
        themes[name] = {
            "names": len(t.result.symbols),
            "missing": len(t.result.missing),
            **subset_row(t),
            "beats_benchmarks": t.comparison["beats_benchmarks"],
        }
    return {
        "note": BREAKOUT_NOTE,
        "selloffs": selloff_rows(ev, inside, settings.robust.selloff_dd_ratio),
        "themes": themes,
    }


def _final_run(
    window: str,
    packet: dict[str, Any],
    spec: StrategySpec,
    *,
    settings: LabSettings | None,
    cfg: AppConfig | None,
    bars: dict[str, pd.DataFrame] | None,
    data_dir: Path | str | None,
    today: date | None,
) -> dict[str, Any]:
    s = settings or load_lab_settings()
    app = cfg or load_config()
    require_validation_machine(window, s, today=today)
    if packet.get("spec_hash") != spec.hash:
        raise NominationError(f"packet is for {packet.get('spec_hash')}, spec hashes to {spec.hash}")
    commit = require_clean_checkout()
    authorize_run(window, spec.hash, settings=s, today=today, data_dir=data_dir)
    if window == "validation":
        frozen = check_frozen_trials(packet.get("frozen_trials"), "packet")
        check_trial_count_order(packet, data_dir=data_dir)
    else:
        frozen = validated_stats(spec.hash, data_dir=data_dir)
    author = packet.get("author_bot") or spec.author_bot or "unknown"
    if bars is None:
        wanted = [s.benchmark, *universe_symbols(spec.universe, settings=s, cfg=app), *theme_symbols(app)]
        bars = load_lab_bars(list(dict.fromkeys(wanted)), settings=s, data_dir=data_dir, today=today)
    intraday = IntradaySource(settings=s, data_dir=data_dir, today=today) if spec.timeframe else None

    recheck = None
    if window == "validation":
        report = run_robust(spec, author_bot=author, settings=s, cfg=app, bars=bars, data_dir=data_dir,
                            today=today, intraday=intraday)
        recheck = recheck_summary(report)
        failures = gate_failures(report, benchmark=s.benchmark, data_dir=data_dir)
        if failures:
            return {"ran": False, "window": window, "spec_id": spec.id, "spec_hash": spec.hash,
                    "reasons": [f"discovery recheck: {x}" for x in failures], "recheck": recheck}

    nomination = {k: packet.get(k) for k in ("spec_hash", "author_bot", "nominated_at", "git_commit")}

    def extra(ev: Evaluation, rw: ResolvedWindow) -> dict[str, Any]:
        out: dict[str, Any] = {"frozen_trials": frozen, "nomination": nomination, "scored_at_commit": commit}
        if recheck is not None:
            out["recheck"] = recheck
        out["breakouts"] = breakouts(spec, ev, rw, bars, s, app, intraday)
        return out

    card, _ = score_spec_full(
        spec, window=window, author_bot=author, frozen_trials=frozen, settings=s, cfg=app,
        bars=bars, data_dir=data_dir, today=today, extra=extra, intraday=intraday,
    )
    return {"ran": True, **card}


def run_validation(
    packet: dict[str, Any],
    spec: StrategySpec,
    *,
    settings: LabSettings | None = None,
    cfg: AppConfig | None = None,
    bars: dict[str, pd.DataFrame] | None = None,
    data_dir: Path | str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Recheck discovery here, then the one validation run (gauntlet step 3)."""
    return _final_run("validation", packet, spec, settings=settings, cfg=cfg, bars=bars,
                      data_dir=data_dir, today=today)


def run_holdout(
    packet: dict[str, Any],
    spec: StrategySpec,
    *,
    settings: LabSettings | None = None,
    cfg: AppConfig | None = None,
    bars: dict[str, pd.DataFrame] | None = None,
    data_dir: Path | str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """The one holdout run (gauntlet step 4), for validation survivors only."""
    return _final_run("holdout", packet, spec, settings=settings, cfg=cfg, bars=bars,
                      data_dir=data_dir, today=today)
