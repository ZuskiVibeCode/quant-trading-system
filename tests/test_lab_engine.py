"""Lab spec engine, runner and CLI on synthetic bars (no network)."""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from quant_system.backtest.cache import save_bars
from quant_system.cli import main
from quant_system.lab.data import bars_dir
from quant_system.lab.engine import run_spec
from quant_system.lab.runner import score_spec
from quant_system.lab.settings import (
    CEILING_ENV,
    DATA_DIR_ENV,
    LabCostSettings,
    LabDataSettings,
    LabSettings,
)
from quant_system.lab.spec import StrategySpec
from quant_system.lab.trials import load_trials
from quant_system.lab.universe import universe_symbols
from quant_system.lab.windows import UNLOCK_ENV, ResolvedWindow, WindowLocked

COSTS = LabCostSettings()
TODAY = date(2026, 10, 5)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (CEILING_ENV, UNLOCK_ENV, "QUANT_LAB_BOT"):
        monkeypatch.delenv(key, raising=False)


def _frame(close, *, start="2021-01-04", dollar_volume=1e9, open_=None) -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    idx = pd.bdate_range(start, periods=len(close))
    o = close if open_ is None else np.asarray(open_, dtype=float)
    return pd.DataFrame(
        {"Open": o, "High": np.maximum(o, close), "Low": np.minimum(o, close), "Close": close,
         "Volume": dollar_volume / close},
        index=idx,
    )


def _random_universe(names, n=400, seed=3, start="2021-01-04"):
    rng = np.random.default_rng(seed)
    out = {"SPY": _frame(100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, n))), start=start)}
    for sym in names:
        c = 30 * np.exp(np.cumsum(rng.normal(0.0003, 0.02, n)))
        out[sym] = _frame(c, start=start, dollar_volume=rng.uniform(5e6, 8e7),
                          open_=c * np.exp(rng.normal(0, 0.005, n)))
    return out


def _spec(**changes) -> StrategySpec:
    base = {"id": "t", "entry": "close > 0", "exit": "bars_held >= 3", "rank": "close",
            "sizing": {"max_positions": 1, "max_weight": 0.2}}
    return StrategySpec.model_validate({**base, **changes})


def _window(bars, first_row, last_row):
    idx = bars["SPY"].index
    return ResolvedWindow("discovery", idx[first_row].date(), idx[last_row].date())


def test_hand_checked_fills_sizing_and_exits():
    bars = {
        "SPY": _frame(np.full(40, 100.0)),
        "AAA": _frame(np.linspace(50, 60, 40)),   # highest close: ranked first
        "BBB": _frame(np.linspace(20, 30, 40)),
        "CCC": _frame(np.linspace(10, 12, 40), dollar_volume=1e6),  # never tradable
    }
    window = _window(bars, 25, 35)
    res = run_spec(_spec(), bars, ["AAA", "BBB", "CCC"], window, COSTS)
    idx = bars["AAA"].index
    t = res.trades
    assert res.first_trade_date == str(idx[26].date())  # signal at day-1 close, fill next open
    assert list(t["symbol"][:3]) == ["AAA", "AAA", "BBB"] and list(t["side"][:3]) == ["buy", "sell", "buy"]
    # Bought at day 26's open, held 3 closes (26, 27, 28), sold at day 29's open.
    assert t["date"].iloc[1] == idx[29] and t["date"].iloc[2] == idx[29]
    aaa = bars["AAA"]
    buy_px = aaa["Open"].iloc[26] * 1.0005
    assert t["shares"].iloc[0] == pytest.approx(0.2 / buy_px)
    proceeds = 0.2 / buy_px * aaa["Open"].iloc[29] * (1 - 0.0005)
    assert t["notional"].iloc[1] == pytest.approx(proceeds)
    # Equity on day 27 = cash + shares x close; returns compound to it.
    eq27 = 0.8 + 0.2 / buy_px * aaa["Close"].iloc[27]
    assert (1 + res.returns.loc[: idx[27]]).prod() == pytest.approx(eq27)
    assert "CCC" not in set(t["symbol"])
    # Cap applies at entry: on each buy day the weight is 20% moved only by open-to-close.
    for _, row in t[t["side"] == "buy"].iterrows():
        bar = bars[row["symbol"]].loc[row["date"]]
        drift = bar["Close"] / (bar["Open"] * 1.0005)
        assert res.weights.at[row["date"], row["symbol"]] == pytest.approx(0.2 * drift, rel=1e-2)
    assert t["date"].max() <= idx[35]
    assert res.returns.index[0] == idx[25] and res.returns.index[-1] == idx[35]


def test_no_fill_when_the_next_open_is_missing():
    bars = {"SPY": _frame(np.full(40, 100.0)), "AAA": _frame(np.linspace(50, 60, 40))}
    idx = bars["AAA"].index
    bars["AAA"] = bars["AAA"].drop(idx[26])
    res = run_spec(_spec(), bars, ["AAA"], _window(bars, 25, 35), COSTS)
    # Day-25 signal can't fill on day 26 (no bar) and day 26 can't signal (no close),
    # so the first fill is day 28's open from day 27's signal; never a same-day price.
    assert res.trades["date"].iloc[0] == idx[28]


def test_engine_has_no_lookahead():
    names = [f"N{i}" for i in range(12)]
    bars = _random_universe(names)
    spec = StrategySpec.model_validate({
        "id": "rsi", "indicators": {"rsi": {"fn": "rsi", "length": 5}, "trend": {"fn": "sma", "length": 50}},
        "entry": "rsi < 40 and close > trend", "exit": "rsi > 60 or bars_held >= 10", "rank": "-rsi",
        "sizing": {"max_positions": 4},
    })
    full = run_spec(spec, bars, names, _window(bars, 60, 399), COSTS)
    cut = 250
    scrambled = {s: df.copy() for s, df in bars.items()}
    for df in scrambled.values():
        df.iloc[cut + 1 :] = df.iloc[cut + 1 :].to_numpy()[::-1]  # rewrite the future
    part = run_spec(spec, scrambled, names, _window(bars, 60, 399), COSTS)
    day = bars["SPY"].index[cut]
    assert full.trade_count > 10
    pd.testing.assert_series_equal(full.returns.loc[:day], part.returns.loc[:day])
    assert not full.returns.loc[day:].equals(part.returns.loc[day:])


def test_constant_exit_rule_and_no_trades_note():
    bars = {"SPY": _frame(np.full(40, 100.0)), "AAA": _frame(np.linspace(50, 60, 40))}
    held = run_spec(_spec(exit="False"), bars, ["AAA"], _window(bars, 25, 35), COSTS)
    assert held.trade_count == 1 and held.weights["AAA"].iloc[-1] > 0
    never = run_spec(_spec(entry="close < 0"), bars, ["AAA"], _window(bars, 25, 35), COSTS)
    assert never.trade_count == 0 and never.notes and (never.returns == 0).all()


def test_score_spec_logs_every_run_and_respects_locks(tmp_path, monkeypatch):
    names = ["IONQ", "RGTI"]
    bars = _random_universe(names, n=1600, start="2016-01-04")
    cfg_names = universe_symbols("quantum")
    bars = {**bars, **{s: bars["IONQ"] for s in cfg_names if s not in bars}}
    spec = _spec(universe="quantum", exit="bars_held >= 5", sizing={"max_positions": 3})
    settings = LabSettings(data=LabDataSettings(ceiling=date(2021, 12, 31)))
    kw = dict(settings=settings, bars=bars, data_dir=tmp_path, today=TODAY)
    card = score_spec(spec, author_bot="tester", **kw)
    assert card["window"] == {"name": "discovery", "start": "2016-01-01", "end": "2021-12-31"}
    assert set(card["benchmarks"]) == {"SPY", "equal_weight"}
    assert card["trials"]["n_trials"] == 1 and 0.0 <= card["deflated_sharpe"] <= 1.0
    assert card["verdict"]["checks"].keys() == {"beats_benchmarks"}
    rec = load_trials(data_dir=tmp_path)[0]
    assert rec["author_bot"] == "tester" and rec["spec_hash"] == spec.hash
    assert "deflated_sharpe" not in rec["metrics"]  # moving target in discovery; shown, not stored
    score_spec(spec, author_bot="tester", **kw)
    assert len(load_trials(data_dir=tmp_path)) == 2 and card["spec_hash"] == spec.hash

    with pytest.raises(ValueError, match="frozen discovery trial stats"):
        score_spec(spec, window="validation", **kw)
    with pytest.raises(WindowLocked, match="locked"):
        score_spec(spec, window="validation", frozen_trials={"n_trials": 5, "sharpe_variance": 0.1}, **kw)


def test_cli_check_and_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    names = universe_symbols("quantum")
    bars = _random_universe(names, n=1600, start="2016-01-04")
    for sym, df in bars.items():
        save_bars(sym, df, bars_dir(tmp_path))
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        "id: cli-test\nuniverse: quantum\n"
        "indicators:\n  rsi: {fn: rsi, length: $n}\n"
        "params:\n  n: {value: 14, min: 7, max: 28}\n"
        "entry: rsi < 35\nexit: rsi > 60 or bars_held >= 15\n",
        encoding="utf-8",
    )
    assert main(["lab", "check", str(spec_path)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert main(["lab", "trials"]) == 0 and json.loads(capsys.readouterr().out)["n_trials"] == 0

    monkeypatch.setenv("QUANT_LAB_BOT", "clibot")
    assert main(["lab", "run", str(spec_path)]) == 0
    card = json.loads(capsys.readouterr().out)
    assert card["author_bot"] == "clibot" and card["strategy"]["days"] > 1000
    assert card["window"]["end"] == "2021-12-31"
    assert main(["lab", "trials"]) == 0 and json.loads(capsys.readouterr().out)["n_trials"] == 1

    bad = tmp_path / "bad.yaml"
    bad.write_text("id: x\nentry: __import__('os')\nexit: 'False'\n", encoding="utf-8")
    assert main(["lab", "check", str(bad)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert main(["lab", "run", str(bad)]) == 1
    capsys.readouterr()
    assert len(load_trials(data_dir=tmp_path)) == 1  # an invalid spec never becomes a trial
