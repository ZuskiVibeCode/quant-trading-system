from quant_system.config.loader import load_config


def test_universe_and_risk():
    cfg = load_config()
    assert cfg.risk.starting_equity == 100.0
    assert cfg.risk.max_position_pct == 0.20
    assert cfg.risk.max_daily_loss_pct == 0.10
    assert cfg.risk.max_drawdown_pct == 0.15
    assert cfg.universe.benchmark == "SPY"
    tickers = cfg.universe.tickers
    assert len(tickers) == 30
    for t in ["IONQ", "RKLB", "NVDA", "TSLA", "IBM", "QBTS", "RDW", "CGNX"]:
        assert t in tickers
    assert cfg.universe.theme_for("IONQ") == "quantum"
    assert cfg.universe.theme_for("QBTS") == "quantum"
    assert cfg.universe.theme_for("RDW") == "space"
    assert cfg.universe.theme_for("CGNX") == "robotics"
