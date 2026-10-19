"""Lab time windows (discovery / validation / holdout) and the rules for running them.

Three layers keep validation and holdout scores away from the bots:

1. Data: bars past the machine's data ceiling are never on disk (``lab.data``),
   so a window whose dates run past the ceiling cannot run there.
2. Unlock: validation and holdout refuse to run unless ``QUANT_LAB_UNLOCK``
   names them (comma-separated).
3. One run: validation and holdout refuse a spec hash already scored in that
   window, and refuse robustness runs entirely (no retuning after discovery).

Window dates are code, not config: moving them is a reviewed change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from quant_system.lab.settings import LabSettings, data_ceiling, load_lab_settings
from quant_system.lab.trials import load_trials

UNLOCK_ENV = "QUANT_LAB_UNLOCK"


@dataclass(frozen=True)
class Window:
    name: str
    start: date
    end: date | None  # None = through this machine's data ceiling ("present")
    locked: bool


WINDOWS: dict[str, Window] = {
    "discovery": Window("discovery", date(2016, 1, 1), date(2021, 12, 31), locked=False),
    "validation": Window("validation", date(2022, 1, 1), date(2024, 12, 31), locked=True),
    "holdout": Window("holdout", date(2025, 1, 1), None, locked=True),
}


class WindowLocked(RuntimeError):
    """A run the window rules do not allow on this machine, now."""


@dataclass(frozen=True)
class ResolvedWindow:
    """A window with a concrete end date, ready to score.

    Strategies may read bars before ``start`` for indicator warm-up, but never
    bars after ``end``: a signal on the last day has no next open, so no fill.
    """

    name: str
    start: date
    end: date

    def mask(self, index: pd.Index) -> pd.Series:
        """By session date, so intraday bars on ``end`` (09:30 and later) are inside."""
        day = pd.DatetimeIndex(index).normalize()
        return pd.Series(
            (day >= pd.Timestamp(self.start)) & (day <= pd.Timestamp(self.end)), index=index
        )

    def sessions(self, index: pd.Index) -> pd.DatetimeIndex:
        idx = pd.DatetimeIndex(index)
        return idx[self.mask(idx).to_numpy()]


def get_window(name: str) -> Window:
    key = name.strip().lower()
    if key not in WINDOWS:
        raise ValueError(f"Unknown window {name!r}; expected one of {', '.join(WINDOWS)}")
    return WINDOWS[key]


def unlocked_windows() -> set[str]:
    raw = os.environ.get(UNLOCK_ENV) or ""
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def resolve_window(
    name: str,
    *,
    settings: LabSettings | None = None,
    today: date | None = None,
) -> ResolvedWindow:
    """Concrete dates for a window, refusing one this machine has no data for."""
    w = get_window(name)
    ceiling = data_ceiling(settings or load_lab_settings(), today=today)
    end = w.end or ceiling
    if end < w.start:
        raise WindowLocked(f"{w.name} starts {w.start}; this machine's data ceiling is {ceiling}")
    if ceiling < end:
        raise WindowLocked(
            f"{w.name} needs bars through {end}; this machine's data ceiling is {ceiling}"
        )
    return ResolvedWindow(w.name, w.start, end)


def authorize_run(
    name: str,
    spec_hash: str,
    *,
    kind: str = "trial",
    settings: LabSettings | None = None,
    today: date | None = None,
    data_dir: Path | str | None = None,
) -> ResolvedWindow:
    """Apply every window rule for one run of one spec; raise ``WindowLocked`` if refused."""
    w = get_window(name)
    if w.locked:
        if w.name not in unlocked_windows():
            raise WindowLocked(
                f"{w.name} is locked; it runs only outside Grok's machine with "
                f"{UNLOCK_ENV}={w.name}"
            )
        if kind != "trial":
            raise WindowLocked(f"{w.name} allows single trial runs only, not {kind} runs")
        prior = load_trials(window=w.name, spec_hash=spec_hash, data_dir=data_dir)
        if prior:
            raise WindowLocked(
                f"spec {spec_hash} already ran on {w.name} ({Path(prior[0]['path']).name}); "
                "one run per spec, no retuning"
            )
    return resolve_window(w.name, settings=settings, today=today)
