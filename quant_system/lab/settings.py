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


class LabDataSettings(BaseModel):
    start: date = date(2016, 1, 1)
    ceiling: date = date(2021, 12, 31)
    feed: str = "sip"
    adjustment: str = "all"


class LabSettings(BaseModel):
    data: LabDataSettings = LabDataSettings()
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
