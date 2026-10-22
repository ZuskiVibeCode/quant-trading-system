"""Strategy spec: what a bot submits instead of code (see docs/lab-spec-format.md)."""

from __future__ import annotations

import re
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from quant_system.lab.indicators import get_indicator
from quant_system.lab.rules import (
    PRICE_FIELDS,
    RESERVED,
    SESSION_NAMES,
    STATE_NAMES,
    Rule,
    RuleError,
    parse_rule,
)
from quant_system.lab.trials import spec_hash

MAX_PARAMS = 4
MAX_INDICATORS = 8
# Live per-position limit (config/risk.yaml max_position_pct); a test keeps them equal.
POSITION_CAP = 0.20
_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,31}$")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Bar sizes the engine can run (all built from Alpaca 15m bars, lab.intraday).
# No timeframe = daily bars. Specs may declare only SPEC_TIMEFRAMES; the others
# exist for the robustness neighbour check and are never scored as trials.
Timeframe = Literal["15m", "30m", "1h", "2h"]
SPEC_TIMEFRAMES = ("15m", "1h")
# Slower bars an intraday spec's indicators may read (multi-timeframe specs):
# 1h bars built from the 15m cache, 1d = the daily cache. Each is seen only once
# fully closed (engine.slower_indicator_panels). Minutes per bar, for ordering.
INDICATOR_TIMEFRAMES = {"1h": 60, "1d": 24 * 60}
_MINUTES = {"15m": 15, "30m": 30, "1h": 60, "2h": 120}


class ParamSpec(BaseModel):
    """One tunable setting: its value and the range a bot considers sensible."""

    model_config = ConfigDict(extra="forbid")
    value: float
    min: float
    max: float

    @model_validator(mode="after")
    def _in_range(self) -> "ParamSpec":
        if not self.min <= self.value <= self.max:
            raise ValueError(f"value {self.value} outside [{self.min}, {self.max}]")
        return self


class SizingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["equal_weight"] = "equal_weight"
    max_positions: int = Field(10, ge=1, le=50)
    max_weight: float = Field(POSITION_CAP, gt=0, le=POSITION_CAP)

    @property
    def target_weight(self) -> float:
        return min(1.0 / self.max_positions, self.max_weight)


class StrategySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    author_bot: str | None = None
    description: str | None = None
    notes: str | None = None
    universe: str = "broad"
    # None = daily. Left out of the canonical form when None, so daily spec hashes
    # are what they were before intraday existed.
    timeframe: Timeframe | None = None
    indicators: dict[str, dict[str, Any]] = Field(default_factory=dict)
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    entry: str
    exit: str
    rank: str | None = None
    sizing: SizingSpec = Field(default_factory=SizingSpec)

    @model_validator(mode="after")
    def _check(self) -> "StrategySpec":
        if not _ID.match(self.id):
            raise ValueError("id: lowercase letters, digits, '-' and '_' only (max 64)")
        if len(self.params) > MAX_PARAMS:
            raise ValueError(f"at most {MAX_PARAMS} tunable settings, got {len(self.params)}")
        if len(self.indicators) > MAX_INDICATORS:
            raise ValueError(f"at most {MAX_INDICATORS} indicators, got {len(self.indicators)}")
        for name in [*self.indicators, *self.params]:
            if not _NAME.match(name) or name in RESERVED:
                raise ValueError(f"name {name!r} is reserved or not a lowercase identifier")
        clash = set(self.indicators) & set(self.params)
        if clash:
            raise ValueError(f"names used for both an indicator and a setting: {sorted(clash)}")

        used: set[str] = set()
        for name, raw in self.indicators.items():
            if "fn" not in raw:
                raise ValueError(f"indicator {name!r} needs fn")
            self._check_indicator_timeframe(name, raw.get("timeframe"))
            args = {k: v for k, v in raw.items() if k not in ("fn", "timeframe")}
            for v in args.values():
                if isinstance(v, str):
                    if not v.startswith("$") or v[1:] not in self.params:
                        raise ValueError(f"indicator {name!r}: {v!r} is not a $setting from params")
                    used.add(v[1:])
            try:
                ind = get_indicator(str(raw["fn"]))
                ind.coerce(self.indicator_args(name))
                # Robustness nudges stay inside a setting's min/max, so both ends must be valid too.
                for key, v in args.items():
                    if isinstance(v, str):
                        p = self.params[v[1:]]
                        for end in (p.min, p.max):
                            ind.coerce({**self.indicator_args(name), key: end})
            except ValueError as exc:
                raise ValueError(f"indicator {name!r}: {exc}") from None

        known = set(self.indicators) | set(self.params) | set(PRICE_FIELDS)
        if self.timeframe:
            known |= set(SESSION_NAMES)
        try:
            rules = self.rules
        except RuleError as exc:
            raise ValueError(f"rule: {exc}") from None
        for field, rule in rules.items():
            allowed = known | (set(STATE_NAMES) if field == "exit" else set())
            unknown = rule.names - allowed
            if unknown:
                hint = " (bars_held only works in exit)" if unknown & set(STATE_NAMES) else ""
                if unknown & set(SESSION_NAMES):
                    hint = f" ({', '.join(SESSION_NAMES)} need an intraday timeframe)"
                raise ValueError(f"{field}: unknown name(s) {sorted(unknown)}{hint}")
            used |= rule.names
        unused = set(self.params) - used
        if unused:
            raise ValueError(f"settings never used: {sorted(unused)}")
        return self

    def _check_indicator_timeframe(self, name: str, tf: Any) -> None:
        if tf is None:
            return
        if tf not in INDICATOR_TIMEFRAMES:
            raise ValueError(
                f"indicator {name!r}: timeframe {tf!r}; an indicator may read "
                f"{' or '.join(INDICATOR_TIMEFRAMES)} bars (leave it out for the spec's own bars)"
            )
        if not self.timeframe:
            raise ValueError(f"indicator {name!r}: a slower timeframe needs an intraday spec timeframe")
        if INDICATOR_TIMEFRAMES[tf] <= _MINUTES[self.timeframe]:
            raise ValueError(
                f"indicator {name!r}: timeframe {tf} is not slower than the spec's {self.timeframe} bars"
            )

    def indicator_timeframe(self, name: str) -> str | None:
        """The slower bar size an indicator reads (1h / 1d), or None for the spec's own bars."""
        return self.indicators[name].get("timeframe")

    @property
    def timeframes(self) -> list[str]:
        """Every bar size the spec reads, fastest first (``1d`` for a daily spec)."""
        own = self.timeframe or "1d"
        slower = {self.indicator_timeframe(n) for n in self.indicators} - {None, own}
        return [own, *sorted(slower, key=INDICATOR_TIMEFRAMES.__getitem__)]

    @cached_property
    def rules(self) -> dict[str, Rule]:
        out = {"entry": parse_rule(self.entry), "exit": parse_rule(self.exit)}
        if self.rank:
            out["rank"] = parse_rule(self.rank)
        return out

    def indicator_args(self, name: str) -> dict[str, Any]:
        """Settings for one indicator with $references replaced by param values."""
        raw = self.indicators[name]
        return {
            k: (self.params[v[1:]].value if isinstance(v, str) else v)
            for k, v in raw.items()
            if k not in ("fn", "timeframe")
        }

    def param_values(self) -> dict[str, float]:
        return {k: p.value for k, p in self.params.items()}

    def with_params(self, values: dict[str, float]) -> "StrategySpec":
        """Same strategy with some settings changed (robustness nudges)."""
        unknown = set(values) - set(self.params)
        if unknown:
            raise ValueError(f"unknown settings {sorted(unknown)}")
        data = self.canonical()
        for k, v in values.items():
            data["params"][k]["value"] = float(v)
        return StrategySpec.model_validate(data)

    def with_timeframe(self, timeframe: str) -> "StrategySpec":
        """Same rules on another bar size (the robustness neighbour check)."""
        return StrategySpec.model_validate({**self.canonical(), "timeframe": timeframe})

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    @property
    def hash(self) -> str:
        return spec_hash(self.canonical())


class SpecError(ValueError):
    """A spec file that can't be loaded or doesn't validate."""


def load_spec(path: Path | str) -> StrategySpec:
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SpecError(f"{p}: {exc}") from None
    if not isinstance(raw, dict):
        raise SpecError(f"{p}: expected a YAML mapping")
    try:
        spec = StrategySpec.model_validate(raw)
    except ValueError as exc:
        raise SpecError(f"{p}: {exc}") from None
    try:
        require_spec_timeframe(spec)
    except ValueError as exc:
        raise SpecError(f"{p}: {exc}") from None
    return spec


def require_spec_timeframe(spec: StrategySpec) -> None:
    """Refuse a timeframe a spec may not declare (30m / 2h are robustness-only)."""
    if spec.timeframe is not None and spec.timeframe not in SPEC_TIMEFRAMES:
        raise ValueError(
            f"timeframe {spec.timeframe!r}: specs may declare {', '.join(SPEC_TIMEFRAMES)} "
            "(or leave it out for daily)"
        )
