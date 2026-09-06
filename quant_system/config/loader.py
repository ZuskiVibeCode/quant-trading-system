"""Load universe + risk YAML config."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class RiskConfig(BaseModel):
    starting_equity: float = 100.0
    paper_mode: bool = True
    max_position_pct: float = 0.20
    max_daily_loss_pct: float = 0.05
    max_drawdown_pct: float = 0.15
    initial_weight: str = "equal"
    weight_floor: float = 0.05
    weight_cap: float = 0.40
    review_30d: str = "equal_only"
    review_90d: str = "light_sharpe"
    review_1y: str = "fuller_sharpe"
    allow_short: bool = False
    min_confidence_to_trade: float = 55.0
    min_order_notional: float = 1.0


class UniverseConfig(BaseModel):
    themes: dict[str, list[str]] = Field(default_factory=dict)
    benchmark: str = "SPY"

    @property
    def tickers(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for names in self.themes.values():
            for t in names:
                u = t.upper()
                if u not in seen:
                    seen.add(u)
                    out.append(u)
        return out

    def theme_for(self, ticker: str) -> str | None:
        u = ticker.upper()
        for theme, names in self.themes.items():
            if u in {n.upper() for n in names}:
                return theme
        return None


class AppConfig(BaseModel):
    risk: RiskConfig
    universe: UniverseConfig
    config_dir: str


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
    key = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID")
    secret = (
        os.environ.get("ALPACA_SECRET_KEY")
        or os.environ.get("ALPACA_API_SECRET")
        or os.environ.get("APCA_API_SECRET_KEY")
    )
    paper = os.environ.get("ALPACA_PAPER", "true").lower() in ("1", "true", "yes")
    return {"api_key": key, "secret_key": secret, "paper": paper, "configured": bool(key and secret)}
