"""Strategy lab core: windows, cost model, benchmarks, metrics, trial log (synthetic data)."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from quant_system.cli import main
from quant_system.lab.benchmarks import benchmark_returns, buy_and_hold
from quant_system.lab.costs import (
    fill_price,
    median_dollar_volume,
    next_open,
    slippage_bps,
    slippage_panel,
    trade_cost,
)
from quant_system.lab.data import bars_panel
from quant_system.lab.metrics import (
    cagr,
    compare,
    deflated_sharpe,
    deflated_sharpe_from_stats,
    expected_max_sharpe,
    exposure,
    max_drawdown,
    pass_bar,
    sharpe,
    turnover,
)
from quant_system.lab.settings import (
    CEILING_ENV,
    DATA_DIR_ENV,
    LabCostSettings,
    LabDataSettings,
    LabSettings,
    load_lab_settings,
)
from quant_system.lab.trials import load_trials, record_trial, spec_hash, trial_stats
from quant_system.lab.windows import (
    UNLOCK_ENV,
    ResolvedWindow,
    WindowLocked,
    authorize_run,
    get_window,
    resolve_window,
)

TODAY = date(2026, 10, 5)
SETTINGS = LabSettings(data=LabDataSettings(ceiling=date(2021, 12, 31)))
COSTS = LabCostSettings()
SPEC = {"id": "rsi-dip", "author_bot": "alpha", "entry": "rsi < 30", "params": {"rsi_len": 14}}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (CEILING_ENV, UNLOCK_ENV):
        monkeypatch.delenv(key, raising=False)


def _bars(start: str, periods: int, *, dollar_volume: float = 1e9, drift: float = 0.001) -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=periods)
    close = 100.0 * (1.0 + drift) ** np.arange(periods)
    open_ = close * 0.999
    return pd.DataFrame(
        {"Open": open_, "High": close, "Low": open_, "Close": close, "Volume": dollar_volume / close},
        index=idx,
    )


def _returns(values, start: str = "2021-01-01") -> pd.Series:
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)), dtype=float)


# --- windows ---------------------------------------------------------------


def test_discovery_resolves_on_the_discovery_ceiling():
    w = resolve_window("Discovery", settings=SETTINGS, today=TODAY)
    assert (w.name, w.start, w.end) == ("discovery", date(2016, 1, 1), date(2021, 12, 31))
    assert authorize_run("discovery", "h", kind="robustness", settings=SETTINGS, today=TODAY) == w


def test_locked_windows_need_unlock_then_data(monkeypatch, tmp_path):
    with pytest.raises(WindowLocked, match="QUANT_LAB_UNLOCK=validation"):
        authorize_run("validation", "h", settings=SETTINGS, today=TODAY, data_dir=tmp_path)
    monkeypatch.setenv(UNLOCK_ENV, "validation")
    with pytest.raises(WindowLocked, match="data ceiling is 2021-12-31"):
        authorize_run("validation", "h", settings=SETTINGS, today=TODAY, data_dir=tmp_path)
    monkeypatch.setenv(CEILING_ENV, "2024-12-31")
    w = authorize_run("validation", "h", settings=SETTINGS, today=TODAY, data_dir=tmp_path)
    assert (w.start, w.end) == (date(2022, 1, 1), date(2024, 12, 31))
    # Unlocking validation does not unlock holdout.
    with pytest.raises(WindowLocked, match="holdout is locked"):
        authorize_run("holdout", "h", settings=SETTINGS, today=TODAY, data_dir=tmp_path)


def test_validation_runs_once_per_spec_and_never_for_robustness(monkeypatch, tmp_path):
    monkeypatch.setenv(UNLOCK_ENV, "validation, holdout")
    monkeypatch.setenv(CEILING_ENV, "today")
    h = spec_hash(SPEC)
    authorize_run("validation", h, settings=SETTINGS, today=TODAY, data_dir=tmp_path)
    record_trial(spec=SPEC, window="validation", metrics={"sharpe": 1.0}, returns=_returns([0.01]),
                 author_bot="alpha", data_dir=tmp_path)
    with pytest.raises(WindowLocked, match="one run per spec"):
        authorize_run("validation", h, settings=SETTINGS, today=TODAY, data_dir=tmp_path)
    # Relabelling the same strategy does not get a second run.
    relabelled = {**SPEC, "id": "rsi-dip-v2", "author_bot": "beta"}
    with pytest.raises(WindowLocked, match="one run per spec"):
        authorize_run("validation", spec_hash(relabelled), settings=SETTINGS, today=TODAY, data_dir=tmp_path)
    with pytest.raises(WindowLocked, match="single trial runs only"):
        authorize_run("validation", "other", kind="robustness", settings=SETTINGS, today=TODAY,
                      data_dir=tmp_path)
    # Holdout is a separate window: the spec still gets its one holdout run.
    w = authorize_run("holdout", h, settings=SETTINGS, today=TODAY, data_dir=tmp_path)
    assert (w.start, w.end) == (date(2025, 1, 1), date(2026, 10, 4))


def test_holdout_refused_when_ceiling_before_it(monkeypatch):
    monkeypatch.setenv(UNLOCK_ENV, "holdout")
    monkeypatch.setenv(CEILING_ENV, "2024-12-31")
    with pytest.raises(WindowLocked, match="holdout starts 2025-01-01"):
        authorize_run("holdout", "h", settings=SETTINGS, today=TODAY)


def test_unknown_window_and_sessions():
    with pytest.raises(ValueError, match="Unknown window"):
        get_window("training")
    w = ResolvedWindow("x", date(2021, 1, 5), date(2021, 1, 7))
    idx = pd.bdate_range("2021-01-01", "2021-01-12")
    assert [d.day for d in w.sessions(idx)] == [5, 6, 7]


# --- costs -----------------------------------------------------------------


def test_slippage_buckets_and_warmup():
    mdv = pd.DataFrame({"A": [np.nan, 60e6, 50e6, 20e6, 10e6, 5e6, 2e6, 1.99e6]})
    bps = slippage_bps(mdv, COSTS)["A"].tolist()
    assert np.isnan(bps[0]) and np.isnan(bps[-1])
    assert bps[1:-1] == [5, 5, 15, 15, 30, 30]


def test_median_dollar_volume_uses_only_bars_up_to_each_day():
    bars = {"A": _bars("2021-01-01", 60, dollar_volume=20e6)}
    bars["A"].loc[bars["A"].index[40:], "Volume"] *= 50  # liquidity jump later
    close, vol = bars_panel(bars, "Close"), bars_panel(bars, "Volume")
    full = median_dollar_volume(close, vol, 20)
    cut = close.index[30]
    trunc = median_dollar_volume(close.loc[:cut], vol.loc[:cut], 20)
    pd.testing.assert_frame_equal(full.loc[:cut], trunc)
    assert full["A"].iloc[:19].isna().all() and full["A"].iloc[19] == pytest.approx(20e6)
    assert slippage_panel(bars, COSTS)["A"].iloc[19] == 15


def test_median_dollar_volume_skips_sessions_a_name_missed():
    close = pd.DataFrame({"A": [10.0, np.nan, 10.0, 10.0]})
    vol = pd.DataFrame({"A": [1.0, np.nan, 3.0, 5.0]})
    out = median_dollar_volume(close, vol, 2)["A"].tolist()
    assert np.isnan(out[0]) and np.isnan(out[1]) and out[2:] == [20.0, 40.0]


def test_next_open_never_falls_back_to_the_same_bar():
    open_ = pd.DataFrame({"A": [1.0, np.nan, 3.0], "B": [5.0, 6.0, 7.0]})
    nxt = next_open(open_)
    assert np.isnan(nxt.at[0, "A"]) and nxt.at[1, "A"] == 3.0  # gap: no fill
    assert nxt.at[0, "B"] == 6.0 and nxt.iloc[-1].isna().all()  # last day: no fill


def test_fill_price_and_trade_cost():
    assert fill_price(100.0, 1, 15) == pytest.approx(100.15)
    assert fill_price(100.0, -1, 15) == pytest.approx(99.85)
    with pytest.raises(ValueError):
        fill_price(100.0, 0, 5)
    traded = pd.DataFrame({"A": [0.5, -0.2], "B": [0.0, 0.0]})
    bps = pd.DataFrame({"A": [10.0, 30.0], "B": [np.nan, np.nan]})
    assert trade_cost(traded, bps).tolist() == pytest.approx([0.0005, 0.0006])
    with pytest.raises(ValueError, match="not tradable"):
        trade_cost(pd.DataFrame({"B": [0.1, 0.0]}), bps)


# --- benchmarks ------------------------------------------------------------


def test_buy_and_hold_enters_at_next_open_after_liquidity_is_known():
    bars = {
        "SPY": _bars("2021-01-01", 80, dollar_volume=1e9),
        "AAA": _bars("2021-01-01", 80, dollar_volume=20e6, drift=0.002),
        "ILL": _bars("2021-01-01", 80, dollar_volume=1e6),
    }
    window = ResolvedWindow("discovery", date(2021, 1, 1), date(2021, 3, 31))
    res = buy_and_hold(bars, ["AAA", "ILL", "NOPE"], window, COSTS)
    days = bars["AAA"].index
    assert res["signal_date"] == str(days[19].date()) and res["entry_date"] == str(days[20].date())
    assert res["tickers"] == ["AAA"] and res["excluded"] == ["ILL", "NOPE"]
    r = res["returns"]
    assert r.index[-1] <= pd.Timestamp("2021-03-31")  # bars past the window end ignored
    assert (r.loc[: days[19]] == 0).all()
    aaa = bars["AAA"]
    expected = aaa["Close"].loc[r.index[-1]] / (aaa["Open"].iloc[20] * 1.0015) - 1.0
    assert (1 + r).prod() - 1 == pytest.approx(expected)


def test_benchmark_uses_last_liquidity_reading_when_signal_day_bar_missing():
    bars = {"SPY": _bars("2021-01-01", 60), "AAA": _bars("2021-01-01", 60, dollar_volume=20e6)}
    window = ResolvedWindow("discovery", date(2021, 1, 1), date(2021, 3, 31))
    signal = bars["AAA"].index[25]
    bars["SPY"] = bars["SPY"].drop(signal)  # SPY skipped the signal session
    res = buy_and_hold(bars, ["SPY"], window, COSTS, signal_day=signal)
    assert res["tickers"] == ["SPY"] and res["entry_date"] == str(bars["AAA"].index[26].date())


def test_benchmarks_share_entry_day_and_use_warmup_before_window():
    bars = {
        "SPY": _bars("2021-11-01", 60, dollar_volume=1e9),
        "AAA": _bars("2021-11-01", 60, dollar_volume=20e6),
    }
    window = ResolvedWindow("validation", date(2022, 1, 1), date(2022, 1, 21))
    out = benchmark_returns(bars, ["AAA"], window, COSTS)
    assert set(out) == {"SPY", "equal_weight"}
    assert out["SPY"]["signal_date"] == out["equal_weight"]["signal_date"] == "2022-01-03"
    assert out["SPY"]["entry_date"] == "2022-01-04"
    assert out["SPY"]["returns"].index[0] == pd.Timestamp("2022-01-03")


# --- metrics ---------------------------------------------------------------


def test_basic_metrics():
    r = _returns([0.001] * 252)
    assert cagr(r) == pytest.approx(1.001**252 - 1)
    assert sharpe(r) == 0.0  # zero variance
    assert max_drawdown(_returns([0.1, -0.5, 0.2])) == pytest.approx(0.5)
    assert max_drawdown(_returns([-0.1])) == pytest.approx(0.1)  # from starting value
    w = pd.DataFrame({"A": [0.5, 0.5, 0.0], "B": [0.0, 0.5, 0.5]})
    assert turnover(w) == pytest.approx((0.5 + 0.5 + 0.5) / 3 * 0.5 * 252)
    assert exposure(w) == pytest.approx((0.5 + 1.0 + 0.5) / 3)


def test_compare_and_pass_bar():
    rng = np.random.default_rng(0)
    bench = _returns(rng.normal(0.0003, 0.01, 500))
    strat = bench * 0.5 + 0.001
    cmp_ = compare(strat, {"SPY": bench, "equal_weight": bench * 0.9})
    assert cmp_["beats_benchmarks"]
    assert cmp_["vs_benchmarks"]["SPY"]["beta"] == pytest.approx(0.5)
    assert pass_bar("discovery", cmp_)["passed"]
    assert not pass_bar("validation", cmp_, deflated=0.90)["passed"]
    assert pass_bar("validation", cmp_, deflated=0.96)["passed"]
    only_spy = compare(strat, {"SPY": bench})
    assert only_spy["beats_benchmarks"] and not pass_bar("discovery", only_spy)["passed"]
    assert not compare(bench, {"SPY": strat})["beats_benchmarks"]


def test_deflated_sharpe_matches_bailey_lopez_de_prado_example():
    # Bailey & Lopez de Prado (2014), numerical example: SR 2.5 annual over 1250
    # days (250/yr), 100 trials with annualized Sharpe variance 0.5, skew -3,
    # kurtosis 10 -> expected max SR ~0.1132 per day, DSR ~0.90.
    assert expected_max_sharpe(100, 0.5 / 250) == pytest.approx(0.1132, abs=1e-4)
    dsr = deflated_sharpe_from_stats(2.5, 1250, -3.0, 10.0, 100, 0.5, periods_per_year=250)
    assert dsr == pytest.approx(0.9004, abs=1e-3)


def test_deflated_sharpe_falls_as_trials_grow():
    r = _returns(np.random.default_rng(1).normal(0.0015, 0.01, 1000))
    one, many = deflated_sharpe(r, 1, 0.5), deflated_sharpe(r, 1000, 0.5)
    assert expected_max_sharpe(1, 0.5) == 0.0
    assert 0.95 < one <= 1.0 and many < one


# --- trial log -------------------------------------------------------------


def test_spec_hash_ignores_labels_not_logic():
    assert spec_hash(SPEC) == spec_hash({**SPEC, "id": "x", "author_bot": "y", "notes": "z"})
    assert spec_hash(SPEC) != spec_hash({**SPEC, "params": {"rsi_len": 15}})


def test_record_and_stats(tmp_path):
    t0 = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
    path = record_trial(spec=SPEC, window="discovery", metrics={"sharpe": 0.5, "dsr": float("nan")},
                        returns=_returns([0.01, -0.02]), author_bot="alpha/1", data_dir=tmp_path, now=t0)
    assert path.name == f"20261005T120000000000Z-alpha_1-{spec_hash(SPEC)}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))  # strict JSON: NaN stored as null
    assert raw["metrics"]["dsr"] is None and raw["returns"]["values"] == [0.01, -0.02]
    assert raw["spec_hash"] == spec_hash(SPEC) and raw["kind"] == "trial"
    assert raw["git_dirty"] in (True, False, None) and "git_commit" in raw
    assert not list(tmp_path.glob("trials/.*"))  # no temp files left behind

    other = {**SPEC, "params": {"rsi_len": 20}}
    # A rerun of the same spec replaces its score; it is not a new trial.
    for i, (spec, s) in enumerate([(SPEC, 1.5), (other, 0.5)], start=1):
        record_trial(spec=spec, window="discovery", metrics={"sharpe": s}, returns=_returns([0.0]),
                     author_bot="beta", data_dir=tmp_path, now=t0.replace(minute=i))
    record_trial(spec={**SPEC, "params": {"rsi_len": 13}}, window="discovery", metrics={"sharpe": 9.0},
                 returns=_returns([0.0]), author_bot="beta", kind="robustness",
                 parent_hash=spec_hash(SPEC), data_dir=tmp_path)
    stats = trial_stats("discovery", data_dir=tmp_path)
    assert stats["n_trials"] == 2 and stats["n_records"] == 3
    assert stats["sharpe_variance"] == pytest.approx(0.5)  # var([1.5, 0.5])
    assert len(load_trials(window="discovery", kind="robustness", data_dir=tmp_path)) == 1
    # A spec with a non-finite Sharpe is still a trial; it just has no Sharpe to spread.
    record_trial(spec={**SPEC, "params": {"rsi_len": 30}}, window="discovery",
                 metrics={"sharpe": float("nan")}, returns=_returns([0.0]), author_bot="beta",
                 data_dir=tmp_path)
    stats = trial_stats("discovery", data_dir=tmp_path)
    assert stats["n_trials"] == 3 and stats["sharpe_variance"] == pytest.approx(0.5)
    assert trial_stats("validation", data_dir=tmp_path)["n_trials"] == 0


def test_record_trial_rejects_bad_input(tmp_path):
    kw = dict(spec=SPEC, window="discovery", returns=_returns([0.0]), author_bot="a", data_dir=tmp_path)
    with pytest.raises(ValueError, match="kind"):
        record_trial(metrics={"sharpe": 1.0}, kind="peek", **kw)
    with pytest.raises(ValueError, match="parent_hash"):
        record_trial(metrics={"sharpe": 1.0}, kind="robustness", **kw)
    with pytest.raises(ValueError, match="sharpe"):
        record_trial(metrics={}, **kw)


def test_cli_lab_trials(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    record_trial(spec=SPEC, window="discovery", metrics={"sharpe": 0.7, "beats_benchmarks": False},
                 returns=_returns([0.0]), author_bot="alpha")
    assert main(["lab", "trials", "--limit", "5"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["n_trials"] == 1 and out["recent"][0]["spec_id"] == "rsi-dip"
    assert out["recent"][0]["sharpe"] == 0.7


def test_repo_lab_settings_cost_buckets():
    buckets = [(b.min_dollar_volume, b.bps) for b in load_lab_settings().costs.buckets]
    assert buckets == [(50e6, 5), (10e6, 15), (2e6, 30)]
    assert load_lab_settings().costs.lookback_days == 20
    assert math.isclose(fill_price(10.0, 1, 5), 10.005)
