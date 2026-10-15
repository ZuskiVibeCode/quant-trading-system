"""Sector / cap-bucket check against an exposure-matched equal-weight (Oct 15, 2026 rule), synthetic bars."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from quant_system.lab.benchmarks import benchmark_returns
from quant_system.lab.metrics import sharpe
from quant_system.lab.robust import matched_returns, run_robust, subset_row
from quant_system.lab.runner import evaluate
from quant_system.lab.settings import CEILING_ENV, LabRobustSettings
from quant_system.lab.spec import StrategySpec
from quant_system.lab.windows import resolve_window
from quant_system.lab_journal import robust_result
from tests.test_lab_robust import NAMES, SPEC, _bars, _settings

TODAY = date(2026, 10, 5)
HOLD_ALL = {"id": "hold", "entry": "close > 0", "exit": "False", "sizing": {"max_positions": 8, "max_weight": 0.125}}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(CEILING_ENV, raising=False)


def _window(settings):
    return resolve_window("discovery", settings=settings, today=TODAY)


def test_equal_weight_legs_compound_to_its_daily_returns(tmp_path):
    settings = _settings(tmp_path)
    ew = benchmark_returns(_bars(NAMES), list(NAMES), _window(settings), settings.costs)["equal_weight"]
    assert np.allclose((1 + ew["overnight"]) * (1 + ew["intraday"]) - 1, ew["returns"])
    assert (ew["overnight"].loc[: ew["entry_date"]] == 0).all()  # cash overnight up to the entry open


def test_open_exposure_and_matched_benchmark_for_a_fully_invested_basket(tmp_path):
    settings = _settings(tmp_path)
    bars = _bars(NAMES)
    ev = evaluate(StrategySpec.model_validate(HOLD_ALL), bars, list(NAMES), _window(settings), settings)
    first = pd.Timestamp(ev.result.first_trade_date)
    assert (ev.result.open_exposure.loc[ev.result.open_exposure.index < first] == 0).all()
    assert ev.result.open_exposure.loc[first:].min() > 0.99
    # Holding exactly the equal-weight basket: matched = equal-weight = the strategy; no edge either way.
    assert ev.benchmarks["equal_weight"]["entry_date"] == ev.result.first_trade_date
    m = matched_returns(ev)
    assert np.allclose(m, ev.benchmarks["equal_weight"]["returns"], atol=1e-6)
    assert abs(subset_row(ev)["active_return"]) < 1e-4

    never = evaluate(StrategySpec.model_validate({"id": "never", "entry": "close < 0", "exit": "False"}),
                     bars, list(NAMES), _window(settings), settings)
    assert (never.result.open_exposure == 0).all() and (matched_returns(never) == 0).all()
    row = subset_row(never)
    assert row["entries"] == 0 and row["beats_matched_equal_weight"] is False


def _fake_eval(basket: pd.Series, strategy: pd.Series, invested: pd.Series, entries: int) -> SimpleNamespace:
    """Enough of an Evaluation for subset_row: all the basket's move happens open to close."""
    ew_sharpe, s_sharpe = sharpe(basket), sharpe(strategy)
    return SimpleNamespace(
        result=SimpleNamespace(
            returns=strategy,
            weights=invested.to_frame("X"),
            open_exposure=invested,
            trades=pd.DataFrame({"side": ["buy"] * entries}),
        ),
        benchmarks={"equal_weight": {"overnight": basket * 0.0, "intraday": basket}},
        strategy={"sharpe": s_sharpe, "cagr": 0.0, "trade_count": 2 * entries, "exposure": float(invested.mean())},
        comparison={"vs_benchmarks": {"equal_weight": {"sharpe": ew_sharpe, "sharpe_diff": s_sharpe - ew_sharpe}}},
    )


def test_matching_exposure_fixes_both_biases_of_the_old_rule():
    rng = np.random.default_rng(42)
    idx = pd.bdate_range("2016-01-04", "2021-12-31")
    noise = rng.normal(0.0, 0.008, len(idx))
    basket = pd.Series(noise - noise.mean() + 0.0007, index=idx)  # a group with Sharpe ~1.4

    # 1. A sparse picker: in the market 20% of days, beating the basket by 3 bp/day while in.
    on = pd.Series(rng.random(len(idx)) < 0.2, index=idx).astype(float)
    picker = on * (basket + 0.0003)
    row = subset_row(_fake_eval(basket, picker, on, entries=150))
    assert sharpe(picker) < sharpe(basket)  # the old rule failed it: cash days dilute its Sharpe
    assert row["beats_equal_weight_sharpe"] is False
    assert row["beats_matched_equal_weight"] is True and row["active_return"] > 0

    # 2. A cash-sitter: holds the basket, nothing better, but sat out the group's slump.
    slump = basket.copy()
    slump.iloc[300:400] = -0.005
    out = pd.Series(1.0, index=idx)
    out.iloc[300:400] = 0.0
    sitter = out * slump
    row = subset_row(_fake_eval(slump, sitter, out, entries=40))
    assert row["beats_equal_weight_sharpe"] is True  # the old rule credited sitting in cash
    assert row["beats_matched_equal_weight"] is False and row["active_return"] == pytest.approx(0, abs=1e-12)


@pytest.mark.parametrize("min_entries", [0, 10_000])
def test_thin_groups_count_as_misses_and_stay_in_the_denominator(tmp_path, min_entries):
    settings = _settings(tmp_path / "cfg")
    settings = settings.model_copy(update={"robust": LabRobustSettings(min_subset_names=2,
                                                                       min_subset_entries=min_entries)})
    rep = run_robust(StrategySpec.model_validate(SPEC), author_bot="t", settings=settings, bars=_bars(NAMES),
                     data_dir=tmp_path, today=TODAY)
    for check, key in (("sectors", "sector"), ("cap_buckets", "cap_bucket")):
        c = rep["checks"][check]
        groups = c["groups"]
        assert len(groups) == 2
        for g in groups:
            assert g["enough_entries"] == (g["entries"] >= min_entries)
            assert g["passes"] == (g["enough_entries"] and g["beats_matched_equal_weight"])
        assert c["pass_share"] == sum(g["passes"] for g in groups) / len(groups)
        assert c["thin"] == [g[key] for g in groups if not g["enough_entries"]]
        if min_entries == 10_000:
            assert c["status"] == "fail" and len(c["thin"]) == 2
    assert rep["thresholds"]["min_subset_entries"] == min_entries

    detail = next(x["detail"] for x in robust_result(rep)["checks"] if x["name"] == "sectors")
    assert "held at the strategy's own exposure" in detail
    assert ("Too few entries" in detail) == (min_entries == 10_000)


def test_journal_keeps_the_old_wording_for_old_reports():
    old = {"checks": {"sectors": {"status": "fail", "groups": [
        {"sector": "Energy", "beats_equal_weight_sharpe": True},
        {"sector": "Tech", "beats_equal_weight_sharpe": False},
    ]}}, "thresholds": {}}
    detail = robust_result(old)["checks"][0]["detail"]
    assert detail.startswith("Beat that group's equal-weight Sharpe in 1 of 2 sectors") and "Tech" in detail
