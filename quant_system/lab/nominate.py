"""Nomination (`lab nominate`): freeze a discovery survivor for its single validation run.

Runs on Grok's machine, from a clean checkout. It reruns the robustness checks
(gauntlet step 2) itself instead of trusting an earlier report, and nominates
only a spec that passes with a complete report. The packet freezes the
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
from quant_system.lab.robust import run_robust
from quant_system.lab.settings import LabSettings
from quant_system.lab.spec import StrategySpec
from quant_system.lab.trials import fresh_git_state, trial_stats, write_json_atomic

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


def gate_failures(report: dict[str, Any]) -> list[str]:
    """Why a robust report can't nominate: the spec must pass and every check must have run."""
    out = []
    if not report["base"]["passed"]:
        out.append("spec does not beat SPY and equal-weight after costs on discovery")
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
    author = author_bot or spec.author_bot or "unknown"
    report = run_robust(
        spec, author_bot=author, settings=settings, cfg=cfg, bars=bars, data_dir=data_dir, today=today
    )
    failures = gate_failures(report)
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
