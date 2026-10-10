"""Daily P&L restarts at each ET trading day; a day-loss halt lifts on the next day."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from quant_system.tracker.api import Tracker

# 2026-10-09 is a Friday; ET midnight is 04:00Z in October (EDT).
OCT8_CLOSE = "2026-10-08T19:58:16.000000Z"
OCT9_INTRADAY = "2026-10-09T15:00:00.000000Z"
OCT9_NOON_ET = datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc)
OCT9_LATE_ET = datetime(2026, 10, 10, 1, 30, tzinfo=timezone.utc)  # 9:30 PM ET, still Oct 9
OCT12_OPEN_ET = datetime(2026, 10, 12, 13, 35, tzinfo=timezone.utc)


@pytest.fixture()
def tracker(tmp_path):
    t = Tracker(str(tmp_path / "roll.db"))
    for key in ("cash", "equity", "peak_equity", "day_start_equity"):
        t.set_state(key, "100.0")
    yield t
    t.close()


def _snapshot_at(tracker: Tracker, equity: float, created_at: str) -> None:
    sid = tracker.snapshot_portfolio(equity=equity, cash=0.0, positions=[])
    tracker.conn.execute("UPDATE portfolio_snapshots SET created_at = ? WHERE id = ?", (created_at, sid))
    tracker.conn.commit()


def test_roll_uses_prior_close_not_starting_equity(tracker):
    _snapshot_at(tracker, 96.85, OCT8_CLOSE)
    _snapshot_at(tracker, 97.61, OCT9_INTRADAY)

    assert tracker.roll_trading_day(now=OCT9_NOON_ET) is True
    assert float(tracker.get_state("day_start_equity")) == pytest.approx(96.85)
    assert tracker.get_state("day_start_date") == "2026-10-09"


def test_roll_is_once_per_et_day(tracker):
    _snapshot_at(tracker, 96.85, OCT8_CLOSE)
    assert tracker.roll_trading_day(now=OCT9_NOON_ET) is True
    tracker.set_state("day_start_equity", "1.0")  # a second roll would overwrite this
    assert tracker.roll_trading_day(now=OCT9_LATE_ET) is False
    assert tracker.get_state("day_start_equity") == "1.0"


def test_next_day_rolls_from_latest_close(tracker):
    _snapshot_at(tracker, 96.85, OCT8_CLOSE)
    tracker.roll_trading_day(now=OCT9_NOON_ET)
    _snapshot_at(tracker, 97.40, "2026-10-09T19:58:58.000000Z")

    assert tracker.roll_trading_day(now=OCT12_OPEN_ET) is True
    assert float(tracker.get_state("day_start_equity")) == pytest.approx(97.40)


def test_roll_without_history_uses_current_equity(tracker):
    tracker.set_state("equity", "93.5")
    assert tracker.roll_trading_day(now=OCT9_NOON_ET) is True
    assert float(tracker.get_state("day_start_equity")) == pytest.approx(93.5)


def test_day_halt_lifts_on_new_day(tracker):
    tracker.halt("Max daily loss breached: -11.00% <= -10%", day_only=True)
    assert tracker.orders_blocked()[0] is True

    tracker.roll_trading_day(now=OCT9_NOON_ET)
    assert tracker.is_halted() is False
    assert tracker.is_day_halted() is False
    assert tracker.orders_blocked() == (False, "")


def test_full_halt_survives_new_day(tracker):
    tracker.halt("Max drawdown breached: 15.20% >= 15%")
    tracker.roll_trading_day(now=OCT9_NOON_ET)
    assert tracker.is_halted() is True
    assert tracker.get_state("halt_reason").startswith("Max drawdown")


def test_full_halt_after_day_halt_is_not_lifted(tracker):
    tracker.halt("Max daily loss breached", day_only=True)
    tracker.halt("manual stop")
    assert tracker.is_day_halted() is False

    tracker.roll_trading_day(now=OCT9_NOON_ET)
    assert tracker.is_halted() is True
    assert tracker.get_state("halt_reason") == "manual stop"


def test_reconcile_daily_pnl_is_against_prior_close(tracker):
    """The Oct 9 export showed daily P&L of -2.60 (= 97.40 - 100 start); it should be +0.55."""
    _snapshot_at(tracker, 96.85, OCT8_CLOSE)
    tracker.set_state("day_start_date", "")  # never rolled, as in production before this fix
    tracker.roll_trading_day(now=OCT9_NOON_ET)

    tracker.reconcile_portfolio(cash=0.22, equity=97.40, positions=[])
    snap = tracker.latest_snapshot()
    assert snap["daily_pnl"] == pytest.approx(0.55)


# --- loss-from-start stop: keeps the 10%-below-$100 protection the stuck day start gave by accident ---


def _pm_at(tracker: Tracker, equity: float, *, day_start: float):
    from quant_system.config.loader import load_config
    from quant_system.execution.portfolio_manager import PortfolioManager
    from quant_system.market_data.yfinance_provider import MockMarketData

    tracker.set_state("cash", f"{equity:.6f}")
    tracker.set_state("day_start_equity", f"{day_start:.6f}")
    cfg = load_config()
    assert cfg.risk.max_loss_from_start_pct == pytest.approx(0.10)
    return PortfolioManager(tracker, MockMarketData(prices={"IONQ": 10.0, "SPY": 100.0}), cfg)


def _one_signal(tracker: Tracker):
    from quant_system.models.signal import Direction, Signal

    sig = Signal(agent_id="momentum", ticker="IONQ", direction=Direction.LONG, confidence=90.0,
                 reasoning="test", supporting_data={})
    return [(tracker.insert_signal(sig), sig)]


def test_loss_from_start_halts_on_a_quiet_day(tracker):
    """Down 10.1% from $100 but only 0.1% on the day: the daily limit passes, the start stop halts."""
    pm = _pm_at(tracker, 89.9, day_start=90.0)
    out = pm.weigh_and_decide(_one_signal(tracker))

    assert tracker.is_halted() is True
    assert tracker.is_day_halted() is False
    assert tracker.get_state("halt_reason").startswith("Max loss from start")
    assert out and "Loss-from-start halt" in out[0].rationale


def test_loss_from_start_halt_survives_the_next_day(tracker):
    pm = _pm_at(tracker, 89.9, day_start=90.0)
    pm.weigh_and_decide(_one_signal(tracker))
    tracker.roll_trading_day(now=OCT12_OPEN_ET)
    assert tracker.is_halted() is True


def test_no_halt_inside_the_limit(tracker):
    pm = _pm_at(tracker, 90.44, day_start=92.0)  # Sep 16 low
    pm.weigh_and_decide(_one_signal(tracker))
    assert tracker.is_halted() is False


def test_backtests_turn_the_start_stop_off():
    from quant_system.backtest.harness import _apply_backtest_risk_overrides
    from quant_system.config.loader import load_config

    cfg = _apply_backtest_risk_overrides(load_config())
    assert cfg.risk.max_loss_from_start_pct is None
    assert load_config().risk.max_loss_from_start_pct == pytest.approx(0.10)


def test_start_stop_fails_safe_when_the_key_is_missing():
    """A risk.yaml without max_loss_from_start_pct keeps the 10% stop on; only an explicit null turns it off."""
    from quant_system.config.loader import RiskConfig

    assert RiskConfig().max_loss_from_start_pct == pytest.approx(0.10)
    assert RiskConfig.model_validate({"starting_equity": 100.0}).max_loss_from_start_pct == pytest.approx(0.10)
    assert RiskConfig.model_validate({"max_loss_from_start_pct": None}).max_loss_from_start_pct is None
