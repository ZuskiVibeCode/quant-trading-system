"""Load universe + risk YAML config."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class RiskConfig(BaseModel):
    starting_equity: float = 100.0
    paper_mode: bool = True
    max_position_pct: float = 0.20
    max_daily_loss_pct: float = 0.10
    max_drawdown_pct: float = 0.15
    initial_weight: str = "equal"
    weight_floor: float = 0.05
    weight_cap: float = 0.40
    review_30d: str = "equal_only"
    review_90d: str = "light_sharpe"
    review_1y: str = "fuller_sharpe"
    allow_short: bool = False
    min_confidence_to_trade: float = 55.0
    min_confidence_to_sell: float = 40.0
    # |blended score| below this → HOLD (live default 0.05; backtest may lower)
    min_abs_score: float = 0.05
    min_order_notional: float = 1.0
    # Cash buffer trim: when cash/equity < min, trim weakest score<=0 holding
    cash_buffer_min_pct: float = 0.03
    cash_buffer_target_pct: float = 0.05  # documented target; trim uses min + frac
    buffer_trim_frac: float = 0.20
    max_buffer_trims_per_cycle: int = 1
    # Opportunity-cost rotate: trim weakest long to fund a higher-score buy when cash is tight
    opportunity_rotate_enabled: bool = True
    opportunity_rotate_min_score_gap: float = 0.15
    opportunity_rotate_trim_frac: float = 0.25
    max_opportunity_trims_per_cycle: int = 2
    opportunity_rotate_min_buy_score: float = 0.05


class ThemeMembers(BaseModel):
    """Per-theme active research set + bench candidates for promotion."""

    active: list[str] = Field(default_factory=list)
    bench: list[str] = Field(default_factory=list)

    @field_validator("active", "bench", mode="before")
    @classmethod
    def _upper_list(cls, v: Any) -> list[str]:
        if v is None:
            return []
        return [str(t).strip().upper() for t in v]


class RotationConfig(BaseModel):
    """Cold/active researcher tunables."""

    lookback_cycles: int = 10
    min_median_confidence: float = 35.0
    max_long_fraction: float = 0.35
    cold_days: int = 15
    max_new_cold_per_theme_per_cycle: int = 1
    active_target_per_theme: int = 10


class UniverseConfig(BaseModel):
    themes: dict[str, ThemeMembers] = Field(default_factory=dict)
    benchmark: str = "SPY"
    rotation: RotationConfig = Field(default_factory=RotationConfig)

    @model_validator(mode="before")
    @classmethod
    def _normalize_themes(cls, data: Any) -> Any:
        """Backward compatible: flat theme lists become active with empty bench."""
        if not isinstance(data, dict):
            return data
        themes = data.get("themes")
        if not isinstance(themes, dict):
            return data
        normalized: dict[str, Any] = {}
        for name, val in themes.items():
            if isinstance(val, list):
                normalized[name] = {"active": val, "bench": []}
            elif isinstance(val, dict):
                # accept legacy keys or missing bench
                if "active" in val or "bench" in val:
                    normalized[name] = {
                        "active": val.get("active") or [],
                        "bench": val.get("bench") or [],
                    }
                elif "tickers" in val and isinstance(val["tickers"], list):
                    normalized[name] = {"active": val["tickers"], "bench": []}
                else:
                    normalized[name] = {"active": [], "bench": []}
            else:
                normalized[name] = {"active": [], "bench": []}
        out = dict(data)
        out["themes"] = normalized
        return out

    @property
    def tickers(self) -> list[str]:
        """Seed/default active tickers (deduped, theme order). Not runtime cold state."""
        out: list[str] = []
        seen: set[str] = set()
        for members in self.themes.values():
            for t in members.active:
                u = t.upper()
                if u not in seen:
                    seen.add(u)
                    out.append(u)
        return out

    @property
    def all_candidates(self) -> list[str]:
        """Full active + bench candidate pool (deduped)."""
        out: list[str] = []
        seen: set[str] = set()
        for members in self.themes.values():
            for t in list(members.active) + list(members.bench):
                u = t.upper()
                if u not in seen:
                    seen.add(u)
                    out.append(u)
        return out

    def theme_for(self, ticker: str) -> str | None:
        u = ticker.upper()
        for theme, members in self.themes.items():
            pool = {n.upper() for n in members.active} | {n.upper() for n in members.bench}
            if u in pool:
                return theme
        return None

    def active_seed(self, theme: str) -> list[str]:
        members = self.themes.get(theme)
        return list(members.active) if members else []

    def bench_seed(self, theme: str) -> list[str]:
        members = self.themes.get(theme)
        return list(members.bench) if members else []


class AppConfig(BaseModel):
    risk: RiskConfig
    universe: UniverseConfig
    config_dir: str


def load_project_dotenv() -> bool:
    """Load repo `.env` into os.environ (setdefault; never overrides).

    Skipped when QUANT_SKIP_DOTENV is truthy (unit tests set this so Alpaca
    isolation is not undone by re-reading a local .env).
    Returns True if a file was loaded.
    """
    if os.environ.get("QUANT_SKIP_DOTENV", "").lower() in ("1", "true", "yes"):
        return False
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]
    for env_path in candidates:
        if not env_path.is_file():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, val)
        return True
    return False


def default_config_dir() -> Path:
    env = os.environ.get("QUANT_CONFIG_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "config"


def load_config(config_dir: Path | str | None = None) -> AppConfig:
    cdir = Path(config_dir) if config_dir else default_config_dir()
    with open(cdir / "risk.yaml") as f:
        risk_raw = yaml.safe_load(f) or {}
    with open(cdir / "universe.yaml") as f:
        uni_raw = yaml.safe_load(f) or {}
    return AppConfig(
        risk=RiskConfig(**risk_raw),
        universe=UniverseConfig(**uni_raw),
        config_dir=str(cdir),
    )


def alpaca_credentials() -> dict[str, Any]:
    load_project_dotenv()
    key = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID")
    secret = (
        os.environ.get("ALPACA_SECRET_KEY")
        or os.environ.get("ALPACA_API_SECRET")
        or os.environ.get("APCA_API_SECRET_KEY")
    )
    paper = os.environ.get("ALPACA_PAPER", "true").lower() in ("1", "true", "yes")
    return {"api_key": key, "secret_key": secret, "paper": paper, "configured": bool(key and secret)}
