"""Nomination (`lab nominate`): freeze a discovery survivor for its single validation run.

Runs on Grok's machine, from a clean checkout. It reruns the robustness checks
(gauntlet step 2) itself instead of trusting an earlier report, and nominates
only a spec that passes with a complete report. The gate reads the spec's own
verdict and ``git_dirty`` from the trial log record that run wrote. The packet freezes the
discovery trial count and Sharpe spread for the deflated Sharpe. It is written
to ``lab_nominations/`` in the checkout and reaches the validation machine by
PR, so the frozen count is reviewed and kept in git history.

The validation machine trusts only the frozen trial stats from the packet. It
recomputes the discovery gate on its own data (``lab.final``).
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from quant_system.config.loader import AppConfig
from quant_system.lab.metrics import pass_bar
from quant_system.lab.robust import run_robust
from quant_system.lab.settings import LabSettings, load_lab_settings
from quant_system.lab.spec import StrategySpec
from quant_system.lab.trials import fresh_git_state, load_trials, trial_stats, write_json_atomic

NOMINATION_KIND = "lab_nomination"
NOMINATION_SCHEMA = 1
_REPO_ROOT = Path(__file__).resolve().parents[2]


class NominationError(ValueError):
    """A nomination, validation or holdout request that can't go ahead."""


def nominations_dir() -> Path:
    return _REPO_ROOT / "lab_nominations"


def require_clean_checkout() -> str | None:
    """The current commit, or ``NominationError`` if the scoring code or config has local edits."""
    commit, dirty = fresh_git_state()
    if dirty is not False:
        state = "can't be checked (git unavailable)" if dirty is None else "has local edits"
        raise NominationError(
            f"this checkout's quant_system/ or config/ {state}; gate runs need a clean, reviewed commit"
        )
    return commit


def gate_failures(
    report: dict[str, Any], *, benchmark: str = "SPY", data_dir: Path | str | None = None
) -> list[str]:
    """Why a robust run can't nominate (empty = it can).

    The spec's own pass/fail and ``git_dirty`` are recomputed from its discovery
    record in the trial log, not taken from the report. The report must also be
    complete (no skipped check), with no failed check, from a clean checkout.
    """
    out = []
    h = report["spec_hash"]
    trial_file = Path(report["base"]["trial_file"]).name
    recs = [
        r for r in load_trials(window="discovery", kind="trial", spec_hash=h, data_dir=data_dir)
        if Path(r["path"]).name == trial_file
    ]
    if not recs:
        out.append(f"the spec's discovery run ({trial_file}) is not in the trial log")
    else:
        rec = recs[0]
        m = rec.get("metrics") or {}
        verdict = pass_bar(
            "discovery",
            {"beats_benchmarks": m.get("beats_benchmarks"), "vs_benchmarks": m.get("vs_benchmarks")},
            required=(benchmark.upper(), "equal_weight"),
        )
        if not verdict["passed"]:
            out.append("spec does not beat SPY and equal-weight after costs on discovery")
        if rec.get("git_dirty") is not False:
            out.append(f"discovery record git_dirty={rec.get('git_dirty')}; needs a clean checkout")
    if report.get("git_dirty") is not False:
        out.append(f"robust report git_dirty={report.get('git_dirty')}; needs a clean checkout")
    if report.get("complete") is not True:
        out.append("robust report complete=false; nomination needs every check to have run")
    for name, check in report["checks"].items():
        if check["status"] == "fail":
            out.append(f"{name}: fail")
        elif check["status"] == "skipped":
            out.append(f"{name}: skipped ({check.get('reason')}); nomination needs a complete report")
    return out


def recheck_summary(report: dict[str, Any]) -> dict[str, Any]:
    base = report["base"]["strategy"]
    return {
        "passed": report["passed"],
        "complete": report["complete"],
        "sharpe": base["sharpe"],
        "cagr": base["cagr"],
        "checks": {k: c["status"] for k, c in report["checks"].items()},
        "robust_report": Path(report["report_file"]).name,
    }


def nominate(
    spec: StrategySpec,
    *,
    author_bot: str | None = None,
    out_dir: Path | str | None = None,
    settings: LabSettings | None = None,
    cfg: AppConfig | None = None,
    bars: dict[str, pd.DataFrame] | None = None,
    data_dir: Path | str | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Rerun the robustness checks and, if they pass completely, write the nomination packet."""
    commit = require_clean_checkout()
    s = settings or load_lab_settings()
    author = author_bot or spec.author_bot or "unknown"
    report = run_robust(
        spec, author_bot=author, settings=s, cfg=cfg, bars=bars, data_dir=data_dir, today=today
    )
    failures = gate_failures(report, benchmark=s.benchmark, data_dir=data_dir)
    result: dict[str, Any] = {
        "nominated": not failures,
        "spec_id": spec.id,
        "spec_hash": spec.hash,
        "robust": recheck_summary(report),
    }
    if failures:
        result["reasons"] = failures
        return result
    # Counted after the robust run, which logs the spec itself as a trial.
    stats = trial_stats("discovery", data_dir=data_dir)
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    packet = {
        "schema": NOMINATION_SCHEMA,
        "kind": NOMINATION_KIND,
        "spec_id": spec.id,
        "spec_hash": spec.hash,
        "author_bot": author,
        "nominated_at": stamp.isoformat(timespec="seconds"),
        "git_commit": commit,
        "frozen_trials": {k: stats[k] for k in ("window", "n_trials", "n_records", "sharpe_variance")},
        "discovery": {
            **recheck_summary(report),
            "vs_benchmarks": {
                k: {"sharpe_diff": v["sharpe_diff"], "excess_cagr": v["excess_cagr"]}
                for k, v in report["base"]["vs_benchmarks"].items()
            },
        },
        "spec": spec.canonical(),
    }
    path = write_json_atomic(Path(out_dir or nominations_dir()) / f"{spec.id}-{spec.hash}.json", packet)
    result.update(packet=str(path), frozen_trials=packet["frozen_trials"])
    return result


def _frozen_ok(frozen: Any) -> bool:
    if not isinstance(frozen, dict) or frozen.get("window") != "discovery":
        return False
    n, var = frozen.get("n_trials"), frozen.get("sharpe_variance")
    return (
        isinstance(n, int) and not isinstance(n, bool) and n >= 1
        and isinstance(var, (int, float)) and math.isfinite(var) and var >= 0
    )


def check_frozen_trials(frozen: Any, source: str) -> dict[str, Any]:
    if not _frozen_ok(frozen):
        raise NominationError(f"{source}: frozen_trials missing or invalid: {frozen!r}")
    return frozen


def load_nomination(path: Path | str) -> tuple[dict[str, Any], StrategySpec]:
    """Read and check a nomination packet; the spec must still validate and match its hash."""
    p = Path(path)
    try:
        packet = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise NominationError(f"{p}: {exc}") from None
    if not isinstance(packet, dict) or packet.get("kind") != NOMINATION_KIND:
        raise NominationError(f"{p}: not a lab nomination packet")
    if packet.get("schema") != NOMINATION_SCHEMA:
        raise NominationError(f"{p}: schema {packet.get('schema')!r}, expected {NOMINATION_SCHEMA}")
    try:
        spec = StrategySpec.model_validate(packet.get("spec"))
    except ValueError as exc:
        raise NominationError(f"{p}: spec does not validate: {exc}") from None
    if spec.hash != packet.get("spec_hash"):
        raise NominationError(
            f"{p}: spec hashes to {spec.hash}, packet says {packet.get('spec_hash')}; edited after nomination?"
        )
    check_frozen_trials(packet.get("frozen_trials"), str(p))
    try:
        nominated_at(packet)
    except (KeyError, TypeError, ValueError):
        raise NominationError(f"{p}: nominated_at missing or not an ISO timestamp") from None
    return packet, spec


def nominated_at(packet: dict[str, Any]) -> datetime:
    stamp = datetime.fromisoformat(str(packet["nominated_at"]))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
