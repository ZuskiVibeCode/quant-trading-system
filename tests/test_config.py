from pathlib import Path

import yaml

from quant_system.config.loader import UniverseConfig, load_config


def test_universe_and_risk():
    cfg = load_config()
    assert cfg.risk.starting_equity == 100.0
    assert cfg.risk.max_position_pct == 0.20
    assert cfg.risk.max_daily_loss_pct == 0.10
    assert cfg.risk.max_drawdown_pct == 0.15
    assert cfg.risk.min_confidence_to_trade == 55.0
    assert cfg.risk.min_confidence_to_sell == 40.0
    assert cfg.risk.cash_buffer_min_pct == 0.03
    assert cfg.risk.cash_buffer_target_pct == 0.05
    assert cfg.risk.buffer_trim_frac == 0.20
    assert cfg.risk.max_buffer_trims_per_cycle == 1
    assert cfg.risk.opportunity_rotate_enabled is True
    assert cfg.risk.opportunity_rotate_min_score_gap == 0.15
    assert cfg.risk.opportunity_rotate_trim_frac == 0.25
    assert cfg.risk.max_opportunity_trims_per_cycle == 2
    assert cfg.risk.opportunity_rotate_min_buy_score == 0.05
    assert cfg.universe.benchmark == "SPY"
    tickers = cfg.universe.tickers
    assert len(tickers) == 30
    for t in ["IONQ", "RKLB", "NVDA", "TSLA", "IBM", "QBTS", "RDW", "CGNX"]:
        assert t in tickers
    assert cfg.universe.theme_for("IONQ") == "quantum"
    assert cfg.universe.theme_for("QBTS") == "quantum"
    assert cfg.universe.theme_for("RDW") == "space"
    assert cfg.universe.theme_for("CGNX") == "robotics"


def test_active_bench_and_rotation_defaults():
    cfg = load_config()
    assert len(cfg.universe.themes["quantum"].active) == 10
    assert cfg.universe.themes["quantum"].bench == ["AMZN", "META", "ORCL", "ACN", "CRWD"]
    assert cfg.universe.themes["space"].bench == ["SPCE", "PL", "JOBY", "KTOS", "HEI"]
    assert cfg.universe.themes["robotics"].bench == ["ADSK", "EMR", "ZBRA", "DE", "ROP"]
    # bench membership via theme_for
    assert cfg.universe.theme_for("AMZN") == "quantum"
    assert cfg.universe.theme_for("JOBY") == "space"
    assert cfg.universe.theme_for("ZBRA") == "robotics"
    candidates = cfg.universe.all_candidates
    assert len(candidates) == 45  # 30 active + 15 bench
    assert "AMZN" in candidates and "AMZN" not in cfg.universe.tickers
    rot = cfg.universe.rotation
    assert rot.lookback_cycles == 10
    assert rot.min_median_confidence == 35.0
    assert rot.max_long_fraction == 0.35
    assert rot.cold_days == 15
    assert rot.max_new_cold_per_theme_per_cycle == 1
    assert rot.active_target_per_theme == 10


def test_legacy_flat_themes_backward_compatible(tmp_path: Path):
    uni = {
        "themes": {
            "quantum": ["IONQ", "IBM"],
            "space": ["RKLB"],
        },
        "benchmark": "SPY",
    }
    (tmp_path / "universe.yaml").write_text(yaml.dump(uni))
    (tmp_path / "risk.yaml").write_text(
        yaml.dump(
            {
                "starting_equity": 100.0,
                "paper_mode": True,
                "max_position_pct": 0.2,
                "max_daily_loss_pct": 0.1,
                "max_drawdown_pct": 0.15,
            }
        )
    )
    cfg = load_config(tmp_path)
    assert cfg.universe.themes["quantum"].active == ["IONQ", "IBM"]
    assert cfg.universe.themes["quantum"].bench == []
    assert cfg.universe.tickers == ["IONQ", "IBM", "RKLB"]
    assert isinstance(cfg.universe, UniverseConfig)
