"""Trial log: one JSON file per lab run, in a directory shared by every bot.

Files live under ``lab_data_dir()/trials`` as ``<utc timestamp>-<bot>-<spec hash>.json``.
One file per run means several bots can write at once without locking; each file
is written to a temp name and renamed, so readers never see a partial record.

The discovery trial count and the spread of trial Sharpes feed the deflated
Sharpe (``metrics.deflated_sharpe``). A trial is a distinct spec hash: rerunning
the same spec is not a new test, and robustness runs (``kind: robustness``) test
one idea under nudged settings, so neither adds to the count.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from quant_system.lab.settings import lab_data_dir

SCHEMA_VERSION = 1
KINDS = ("trial", "robustness")
# Spec fields that label a strategy without changing what it does.
HASH_EXCLUDED_FIELDS = frozenset({"id", "author_bot", "description", "notes"})
_REPO_ROOT = Path(__file__).resolve().parents[2]


def trials_dir(data_dir: Path | str | None = None) -> Path:
    """``<lab data dir>/trials``; ``data_dir`` overrides the lab data root."""
    return (Path(data_dir) if data_dir else lab_data_dir()) / "trials"


def spec_hash(spec: Mapping[str, Any]) -> str:
    """Stable hash of what a spec does (labels like id/author_bot excluded)."""
    body = {k: v for k, v in spec.items() if k not in HASH_EXCLUDED_FIELDS}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _jsonable(obj: Any) -> Any:
    """NaN/inf → None (strict JSON), numpy scalars → Python, recursively."""
    if isinstance(obj, Mapping):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        obj = obj.item()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_") or "unknown"


def record_trial(
    *,
    spec: Mapping[str, Any],
    window: str,
    metrics: Mapping[str, Any],
    returns: pd.Series,
    author_bot: str,
    kind: str = "trial",
    parent_hash: str | None = None,
    extra: Mapping[str, Any] | None = None,
    data_dir: Path | str | None = None,
    now: datetime | None = None,
) -> Path:
    """Write one trial record and return its path.

    ``metrics`` must carry the annualized ``sharpe``; ``returns`` is the daily
    strategy return series for the window (kept for deflated Sharpe and later
    re-analysis). Robustness runs name the spec they nudge via ``parent_hash``.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    if kind == "robustness" and not parent_hash:
        raise ValueError("robustness runs need parent_hash")
    if not isinstance(metrics.get("sharpe"), (int, float)):
        raise ValueError("metrics must include a numeric 'sharpe'")
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    h = spec_hash(spec)
    record = {
        "schema": SCHEMA_VERSION,
        "recorded_at": stamp.isoformat(timespec="microseconds"),
        "kind": kind,
        "window": window,
        "spec_hash": h,
        "parent_hash": parent_hash,
        "spec_id": spec.get("id"),
        "author_bot": author_bot,
        "git_commit": git_commit(),
        "spec": dict(spec),
        "metrics": dict(metrics),
        "returns": {
            "dates": [str(pd.Timestamp(d).date()) for d in returns.index],
            "values": [float(v) for v in returns.values],
        },
        **dict(extra or {}),
    }
    tdir = trials_dir(data_dir)
    tdir.mkdir(parents=True, exist_ok=True)
    name = f"{stamp.strftime('%Y%m%dT%H%M%S%fZ')}-{_slug(author_bot)}-{h}.json"
    path = tdir / name
    tmp = tdir / f".{name}.tmp"
    tmp.write_text(json.dumps(_jsonable(record), indent=1, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_trials(
    *,
    window: str | None = None,
    kind: str | None = None,
    spec_hash: str | None = None,
    data_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Trial records (oldest first), optionally filtered. Each carries its ``path``."""
    tdir = trials_dir(data_dir)
    if not tdir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(tdir.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if window and rec.get("window") != window:
            continue
        if kind and rec.get("kind") != kind:
            continue
        if spec_hash and rec.get("spec_hash") != spec_hash:
            continue
        rec["path"] = str(path)
        out.append(rec)
    out.sort(key=lambda r: r.get("recorded_at", ""))
    return out


def trial_stats(window: str = "discovery", *, data_dir: Path | str | None = None) -> dict[str, Any]:
    """Distinct-spec trial count and the variance of their annualized Sharpes.

    The latest record per spec hash stands for that spec (a rerun after an
    engine fix replaces the earlier score rather than adding a trial).
    """
    records = load_trials(window=window, kind="trial", data_dir=data_dir)
    latest: dict[str, float] = {}
    for rec in records:
        sharpe = (rec.get("metrics") or {}).get("sharpe")
        if isinstance(sharpe, (int, float)) and math.isfinite(sharpe):
            latest[rec["spec_hash"]] = float(sharpe)
    values = list(latest.values())
    variance = float(pd.Series(values).var(ddof=1)) if len(values) >= 2 else 0.0
    return {
        "window": window,
        "n_trials": len(values),
        "n_records": len(records),
        "sharpe_variance": variance,
    }
