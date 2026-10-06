"""Lab universes: the broad test set (config/lab_universe.yaml) and the theme names."""

from __future__ import annotations

from typing import Any

import yaml

from quant_system.config.loader import AppConfig, load_config
from quant_system.lab.settings import LabSettings, load_lab_settings

UNIVERSE_NAMES = ("broad", "themes", "all")


def load_broad_universe(settings: LabSettings | None = None) -> list[dict[str, Any]]:
    """Rows of {symbol, sector, cap_bucket, ...} from the generated universe file."""
    s = settings or load_lab_settings()
    path = s.universe_path
    if not path.is_file():
        raise FileNotFoundError(f"{path} missing; run scripts/build_lab_universe.py")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("tickers") or [])


def theme_symbols(cfg: AppConfig, theme: str | None = None) -> list[str]:
    """Active + bench names for one theme, or all themes (config/universe.yaml)."""
    names = [theme] if theme else list(cfg.universe.themes)
    out: list[str] = []
    for name in names:
        members = cfg.universe.themes.get(name)
        if members is None:
            raise ValueError(f"Unknown theme: {name}")
        out.extend(members.active + members.bench)
    return list(dict.fromkeys(out))


def universe_symbols(
    name: str,
    *,
    settings: LabSettings | None = None,
    cfg: AppConfig | None = None,
) -> list[str]:
    """Symbols for a named universe: broad | themes | all | <theme name>."""
    key = name.strip().lower()
    if key == "broad":
        return [str(r["symbol"]).upper() for r in load_broad_universe(settings)]
    app = cfg or load_config()
    if key == "themes":
        return theme_symbols(app)
    if key == "all":
        broad = universe_symbols("broad", settings=settings, cfg=app)
        return list(dict.fromkeys(broad + theme_symbols(app)))
    return theme_symbols(app, key)
