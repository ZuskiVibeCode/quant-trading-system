"""Indicator registry: pure functions registered with a decorator, plus the lookahead check.

An indicator takes one ticker's bars (columns Open, High, Low, Close, Volume and
BenchClose, the benchmark's close on the same sessions) and returns one float
Series on the same index. No I/O, no global state, and the value on day t may
use only bars up to and including t. ``lookahead_violations`` checks that for
any indicator; the test suite runs it on every registered one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd

BAR_COLUMNS = ("Open", "High", "Low", "Close", "Volume", "BenchClose")


@dataclass(frozen=True)
class ParamDef:
    default: int | float
    lo: int | float
    hi: int | float

    @property
    def is_int(self) -> bool:
        return isinstance(self.default, int)


@dataclass(frozen=True)
class IndicatorDef:
    name: str
    fn: Callable[..., pd.Series]
    params: dict[str, ParamDef]
    doc: str

    def coerce(self, given: dict[str, Any]) -> dict[str, int | float]:
        """Defaults filled in, ints rounded, ranges enforced; unknown names rejected."""
        unknown = set(given) - set(self.params)
        if unknown:
            raise ValueError(f"{self.name}: unknown setting(s) {sorted(unknown)}; has {sorted(self.params)}")
        out: dict[str, int | float] = {}
        for key, p in self.params.items():
            raw = given.get(key, p.default)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
                raise ValueError(f"{self.name}.{key} must be a number, got {raw!r}")
            val = int(round(raw)) if p.is_int else float(raw)
            if not p.lo <= val <= p.hi:
                raise ValueError(f"{self.name}.{key}={val} outside [{p.lo}, {p.hi}]")
            out[key] = val
        return out

    def __call__(self, bars: pd.DataFrame, **params: Any) -> pd.Series:
        out = self.fn(bars, **self.coerce(params))
        if not isinstance(out, pd.Series) or not out.index.equals(bars.index):
            raise TypeError(f"indicator {self.name} must return a Series on the bars' index")
        return out.astype(float)


REGISTRY: dict[str, IndicatorDef] = {}


def indicator(name: str, **params: tuple[int | float, int | float, int | float]):
    """Register ``fn(bars, **settings) -> Series``; each setting is (default, min, max)."""

    def deco(fn: Callable[..., pd.Series]) -> Callable[..., pd.Series]:
        if name in REGISTRY:
            raise ValueError(f"indicator {name!r} already registered")
        defs = {k: ParamDef(*v) for k, v in params.items()}
        doc = (fn.__doc__ or "").strip().splitlines()
        REGISTRY[name] = IndicatorDef(name, fn, defs, doc[0] if doc else "")
        return fn

    return deco


def get_indicator(name: str) -> IndicatorDef:
    if name not in REGISTRY:
        raise ValueError(f"unknown indicator {name!r}; registered: {', '.join(sorted(REGISTRY))}")
    return REGISTRY[name]


def lookahead_violations(
    ind: IndicatorDef,
    bars: pd.DataFrame,
    *,
    params: dict[str, Any] | None = None,
    cutoffs: list[int] | None = None,
) -> list[str]:
    """Days where the value changes once later bars exist (any entry = lookahead).

    For each cut-off, values through the cut-off must be identical whether the
    indicator saw the full history or history truncated at the cut-off. Also
    flags an indicator that modifies its input.
    """
    params = params or {}
    snapshot = bars.copy()
    full = ind(bars, **params)
    problems: list[str] = []
    if not bars.equals(snapshot):
        problems.append(f"{ind.name} modified its input bars")
    n = len(bars)
    for cut in cutoffs or [n // 4, n // 2, (3 * n) // 4, n - 2]:
        part = ind(bars.iloc[: cut + 1], **params)
        a = full.iloc[: cut + 1].to_numpy(dtype=float)
        b = part.to_numpy(dtype=float)
        same = np.isclose(a, b, rtol=1e-9, atol=1e-12, equal_nan=True)
        if not same.all():
            first = bars.index[int(np.argmin(same))]
            problems.append(f"{ind.name}{params or ''}: value on {first.date()} changes when bars after {bars.index[cut].date()} are added")
    return problems
