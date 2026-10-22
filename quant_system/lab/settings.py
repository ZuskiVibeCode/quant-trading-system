"""Load config/lab.yaml and resolve machine-level overrides (data dir, data ceiling)."""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel

from quant_system.config.loader import default_config_dir

DATA_DIR_ENV = "QUANT_LAB_DATA_DIR"
CEILING_ENV = "QUANT_LAB_DATA_CEILING"


class LabWarmupSettings(BaseModel):
    """Bars before ``data.start`` (Alpaca has none) for indicator and cost-model warm-up only.

    Fetched from yfinance for ``start`` .. ``overlap_end``. The days from
    ``data.start`` to ``overlap_end`` are in both sources and only line the two
    up; a name whose sources disagree there gets no warm-up.
    """

    start: date = date(2014, 1, 1)
    overlap_end: date = date(2016, 1, 31)
    # Fewer shared days than this, and the sources can't be compared: no warm-up.
    min_overlap_days: int = 10
    # Each shared day's Alpaca / yfinance close ratio within this of the median ratio.
    price_tolerance: float = 0.01
    # Median Alpaca / yfinance volume ratio within [1 / x, x] (the cost model reads volume).
    volume_ratio_max: float = 1.5


class LabCoverageSettings(BaseModel):
    """Intraday coverage flags (`lab data intraday-status`). A flagged name is listed, not dropped.

    Intraday runs do leave out each name's short sessions and sessions off the
    daily bar by over ``price_tolerance`` (``intraday.session_problems``).
    """

    # Share of a name's scheduled 15m bars with no bar (no trade in that slot).
    max_missing_share: float = 0.05
    # Sessions with no regular-hours bar at all while the daily cache has a bar.
    max_empty_sessions: int = 5
    # Sessions with some bars but under half their scheduled 15m bars (Alpaca has
    # market-wide gaps on a few dates, e.g. 2021-04-19: often just the opening bar).
    max_short_sessions: int = 5
    # A session's first 15m open / last 15m close vs the daily bar, after scaling
    # by the median ratio (the two caches can be adjusted a few days apart).
    price_tolerance: float = 0.01
    max_mismatch_days: int = 5


class LabIntradaySettings(BaseModel):
    """Alpaca 15m bars, regular hours only; 30m / 1h / 2h are built from them."""

    coverage: LabCoverageSettings = LabCoverageSettings()


class LabDataSettings(BaseModel):
    start: date = date(2016, 1, 1)
    ceiling: date = date(2021, 12, 31)
    feed: str = "sip"
    adjustment: str = "all"
    warmup: LabWarmupSettings = LabWarmupSettings()
    intraday: LabIntradaySettings = LabIntradaySettings()


class CostBucket(BaseModel):
    min_dollar_volume: float
    bps: float


class LabIntradayCostSettings(BaseModel):
    """Intraday fills: continuous-market spread, so dearer than the daily open.

    Same median-dollar-volume buckets (from daily bars, as of the previous
    session's close); a fill within ``open_window_minutes`` after the session
    opens, other than the opening print itself, pays ``open_window_multiplier``.
    """

    buckets: list[CostBucket] = [
        CostBucket(min_dollar_volume=50_000_000, bps=8),
        CostBucket(min_dollar_volume=10_000_000, bps=20),
        CostBucket(min_dollar_volume=2_000_000, bps=40),
    ]
    open_window_minutes: int = 30
    open_window_multiplier: float = 1.5


class LabCostSettings(BaseModel):
    """Slippage per side by median dollar volume; below the lowest bucket = not tradable."""

    lookback_days: int = 20
    buckets: list[CostBucket] = [
        CostBucket(min_dollar_volume=50_000_000, bps=5),
        CostBucket(min_dollar_volume=10_000_000, bps=15),
        CostBucket(min_dollar_volume=2_000_000, bps=30),
    ]
    intraday: LabIntradayCostSettings = LabIntradayCostSettings()


class Selloff(BaseModel):
    name: str
    start: date
    end: date


class LabRobustSettings(BaseModel):
    """Gauntlet step 2 (`lab robust`) thresholds; see docs/harness-upgrade-design.md section 9."""

    nudges: list[float] = [-0.2, -0.1, 0.1, 0.2]
    nudge_pass_share: float = 0.75
    peak_ratio: float = 0.5
    min_subset_names: int = 10
    # A sector / cap bucket rerun with fewer entries than this counts as a miss.
    min_subset_entries: int = 30
    sector_pass_share: float = 0.6
    cap_pass_share: float = 0.6
    selloffs: list[Selloff] = [
        Selloff(name="q4_2018", start=date(2018, 10, 1), end=date(2018, 12, 31)),
        Selloff(name="covid_2020", start=date(2020, 2, 1), end=date(2020, 4, 30)),
    ]
    selloff_dd_ratio: float = 1.25
    # Intraday specs only: the same spec rerun on nearby bar sizes. Every neighbour
    # must still beat SPY and equal-weight, and their median Sharpe must be at least
    # peak_ratio x the spec's. A check, never a way to pick the better timeframe.
    timeframe_neighbors: dict[str, list[str]] = {"15m": ["30m"], "1h": ["30m", "2h"]}


class LabLockedSettings(BaseModel):
    """Validation / holdout breakouts (`lab validate`, `lab holdout`). Reported, not in the pass bar."""

    selloffs: list[Selloff] = [Selloff(name="bear_2022", start=date(2022, 1, 3), end=date(2022, 10, 31))]


class LabSettings(BaseModel):
    data: LabDataSettings = LabDataSettings()
    costs: LabCostSettings = LabCostSettings()
    robust: LabRobustSettings = LabRobustSettings()
    locked: LabLockedSettings = LabLockedSettings()
    benchmark: str = "SPY"
    universe_file: str = "lab_universe.yaml"
    config_dir: str = ""

    @property
    def universe_path(self) -> Path:
        return Path(self.config_dir) / self.universe_file


def load_lab_settings(config_dir: Path | str | None = None) -> LabSettings:
    cdir = Path(config_dir) if config_dir else default_config_dir()
    path = cdir / "lab.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return LabSettings(**(raw or {}), config_dir=str(cdir))


def lab_data_dir() -> Path:
    """Shared bar cache + trial log root. One per machine, outside bot checkouts."""
    env = os.environ.get(DATA_DIR_ENV)
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "data" / "lab"


def data_ceiling(settings: LabSettings, *, today: date | None = None) -> date:
    """Last date any lab bar may carry on this machine.

    ``QUANT_LAB_DATA_CEILING`` overrides the YAML value (ISO date, or "today" for
    the latest completed session). Never later than yesterday: today's bar is not
    final, and Alpaca's free plan refuses the most recent SIP data.
    """
    yesterday = (today or date.today()) - timedelta(days=1)
    env = (os.environ.get(CEILING_ENV) or "").strip().lower()
    if env == "today":
        ceiling = yesterday
    elif env:
        ceiling = date.fromisoformat(env)
    else:
        ceiling = settings.data.ceiling
    return min(ceiling, yesterday)
