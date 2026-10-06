"""`lab robust` (gauntlet step 2) on synthetic bars (no network)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from quant_system.backtest.cache import save_bars
from quant_system.cli import main
from quant_system.lab.data import bars_dir
from quant_system.lab.robust import nudge_check, nudged_values, run_robust
from quant_system.lab.settings import CEILING_ENV, DATA_DIR_ENV, LabRobustSettings, load_lab_settings
from quant_system.lab.spec import StrategySpec
from quant_system.lab.trials import load_trials, trial_stats
from quant_system.lab.universe import universe_symbols
from quant_system.lab.windows import UNLOCK_ENV

TODAY = date(2026, 10, 5)
NAMES = {  # symbol: (sector, cap bucket)
    "AAA": ("Tech", "large"), "BBB": ("Tech", "large"), "CCC": ("Tech", "mid"), "DDD": ("Tech", "mid"),
    "EEE": ("Energy", "large"), "FFF": ("Energy", "large"), "GGG": ("Energy", "mid"), "HHH": ("Energy", "mid"),
}
SPEC = {
    "id": "rsi-dip",
    "indicators": {"rsi": {"fn": "rsi", "length": "$n"}},
    "params": {"n": {"value": 14, "min": 7, "max": 28}},
    "entry": "rsi < 40",
    "exit": "rsi > 60 or bars_held >= 10",
    "sizing": {"max_positions": 4},
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (CEILING_ENV, UNLOCK_ENV, "QUANT_LAB_BOT"):
        monkeypatch.delenv(key, raising=False)


def _bars(symbols, seed=5) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2016-01-04", "2021-12-31")
    mkt = np.cumsum(rng.normal(0.0004, 0.01, len(idx)))
    out = {}
    for sym in ["SPY", *symbols]:
        c = 40 * np.exp(mkt + (0 if sym == "SPY" else np.cumsum(rng.normal(0, 0.015, len(idx)))))
        o = c * np.exp(rng.normal(0, 0.004, len(idx)))
        out[sym] = pd.DataFrame(
            {"Open": o, "High": np.maximum(o, c), "Low": np.minimum(o, c), "Close": c, "Volume": 1e8 / c},
            index=idx,
        )
    return out


def _settings(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "lab.yaml").write_text("universe_file: u.yaml\n", encoding="utf-8")
    rows = [{"symbol": s, "sector": sec, "cap_bucket": cap} for s, (sec, cap) in NAMES.items()]
    (tmp_path / "u.yaml").write_text(yaml.safe_dump({"tickers": rows}), encoding="utf-8")
    s = load_lab_settings(tmp_path)
    return s.model_copy(update={"robust": LabRobustSettings(min_subset_names=2)})


def test_nudged_values_clip_to_the_spec_range_and_dedupe():
    spec = StrategySpec.model_validate({**SPEC, "params": {"n": {"value": 26, "min": 7, "max": 28}}})
    assert list(nudged_values(spec, [-0.2, -0.1, 0.1, 0.2])) == [
        ("n", -0.2, 20.8), ("n", -0.1, 23.4), ("n", 0.1, 28.0)  # +20% clips to 28 too: dropped
    ]


def test_nudge_check_fails_a_sharp_peak():
    r = LabRobustSettings()
    good = [{"sharpe": 0.9, "passes": True}] * 4
    assert nudge_check(good, 1.0, r)["status"] == "pass"
    assert nudge_check([{"sharpe": 0.3, "passes": True}] * 4, 1.0, r)["status"] == "fail"  # peak
    assert nudge_check(good[:2] + [{"sharpe": 0.9, "passes": False}] * 2, 1.0, r)["status"] == "fail"
    assert nudge_check(good, -0.1, r)["status"] == "fail"
    assert nudge_check([], 1.0, r)["status"] == "skipped"


def test_run_robust_logs_variants_under_the_spec(tmp_path):
    settings = _settings(tmp_path / "cfg")
    spec = StrategySpec.model_validate(SPEC)
    rep = run_robust(spec, author_bot="bot1", settings=settings, bars=_bars(NAMES),
                     data_dir=tmp_path, today=TODAY)

    checks = rep["checks"]
    assert set(checks) == {"nudges", "sectors", "cap_buckets", "selloffs", "single_stock"}
    assert [v["value"] for v in checks["nudges"]["variants"]] == [11.2, 12.6, 15.4, 16.8]
    assert {g["sector"] for g in checks["sectors"]["groups"]} == {"Energy", "Tech"}
    assert {g["cap_bucket"] for g in checks["cap_buckets"]["groups"]} == {"large", "mid"}
    assert [p["name"] for p in checks["selloffs"]["periods"]] == ["q4_2018", "covid_2020"]
    assert rep["complete"] is (checks["single_stock"]["status"] != "skipped")
    statuses = {c["status"] for c in checks.values()}
    assert rep["passed"] == (rep["base"]["passed"] and "fail" not in statuses)

    # The spec is one trial; every variant is a robustness record under it.
    assert trial_stats("discovery", data_dir=tmp_path)["n_trials"] == 1
    variants = load_trials(kind="robustness", data_dir=tmp_path)
    runs = 4 + 2 + 2 + (checks["single_stock"]["status"] != "skipped")
    assert len(variants) == runs
    assert {v["parent_hash"] for v in variants} == {spec.hash}
    assert {v["check"] for v in variants} >= {"nudge", "sectors", "cap_buckets"}

    saved = json.loads(Path(rep["report_file"]).read_text(encoding="utf-8"))
    assert saved["thresholds"]["min_subset_names"] == 2 and saved["spec_hash"] == spec.hash
    assert "git_dirty" in saved


def test_selloff_check_compares_drawdowns(tmp_path):
    settings = _settings(tmp_path / "cfg")
    bars = _bars(NAMES)
    # Make every stock crash 60% in March 2020 while SPY dips less.
    crash = (bars["AAA"].index >= "2020-03-02") & (bars["AAA"].index <= "2020-03-31")
    for sym in NAMES:
        df = bars[sym]
        factor = np.where(crash, 0.97, 1.0).cumprod()
        bars[sym] = df.mul(np.r_[factor], axis=0).assign(Volume=df["Volume"])
    always_in = StrategySpec.model_validate({"id": "hold", "entry": "close > 0", "exit": "False",
                                             "sizing": {"max_positions": 8, "max_weight": 0.125}})
    rep = run_robust(always_in, settings=settings, bars=bars, data_dir=tmp_path, today=TODAY)
    covid = next(p for p in rep["checks"]["selloffs"]["periods"] if p["name"] == "covid_2020")
    # Fully invested in the same names as equal-weight: drawdowns match, so it passes.
    assert covid["max_drawdown"] == pytest.approx(covid["benchmark_drawdowns"]["equal_weight"], rel=0.05)
    assert covid["ok"]

    # Concentrated in the one name that crashes 80%: far worse than SPY or equal-weight.
    bars = _bars(NAMES)
    crash = (bars["AAA"].index >= "2020-03-02") & (bars["AAA"].index <= "2020-03-31")
    df = bars["AAA"]
    bars["AAA"] = df.mul(10 * np.where(crash, 0.93, 1.0).cumprod(), axis=0).assign(Volume=df["Volume"] / 10)
    one = StrategySpec.model_validate({"id": "one", "entry": "close > 0", "exit": "False", "rank": "close",
                                       "sizing": {"max_positions": 1, "max_weight": 0.2}})
    rep = run_robust(one, settings=settings, bars=bars, data_dir=tmp_path, today=TODAY)
    covid = next(p for p in rep["checks"]["selloffs"]["periods"] if p["name"] == "covid_2020")
    worst = max(covid["benchmark_drawdowns"].values())
    assert covid["max_drawdown"] > 1.25 * worst and not covid["ok"]
    assert rep["checks"]["selloffs"]["status"] == "fail" and not rep["passed"]


def test_skipped_checks_make_the_report_incomplete(tmp_path):
    names = universe_symbols("quantum")
    spec = StrategySpec.model_validate({"id": "never", "universe": "quantum", "entry": "close < 0",
                                        "exit": "False"})
    rep = run_robust(spec, bars=_bars(names), data_dir=tmp_path, today=TODAY)
    st = {k: c["status"] for k, c in rep["checks"].items()}
    assert st == {"nudges": "skipped", "sectors": "skipped", "cap_buckets": "skipped",
                  "selloffs": "pass", "single_stock": "skipped"}
    assert rep["complete"] is False and rep["passed"] is False  # base never trades, can't pass
    assert load_trials(kind="robustness", data_dir=tmp_path) == []


def test_cli_lab_robust(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    names = universe_symbols("quantum")
    for sym, df in _bars(names).items():
        save_bars(sym, df, bars_dir(tmp_path))
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump({**SPEC, "universe": "quantum"}), encoding="utf-8")
    assert main(["lab", "robust", str(path), "--bot", "clibot"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["author_bot"] == "clibot" and rep["checks"]["sectors"]["status"] == "skipped"
    assert len(rep["checks"]["nudges"]["variants"]) == 4
    assert main(["lab", "trials"]) == 0 and json.loads(capsys.readouterr().out)["n_trials"] == 1
