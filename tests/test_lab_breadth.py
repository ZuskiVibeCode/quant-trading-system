"""Math and no-lookahead checks for breadth_above_sma50 / engine Breadth50 injection."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_system.lab.engine import (
    breadth_above_sma,
    clear_breadth_cache,
    indicator_panels,
    run_spec,
)
from quant_system.lab.indicators import REGISTRY, lookahead_violations
from quant_system.lab.settings import LabCostSettings
from quant_system.lab.spec import StrategySpec
from quant_system.lab.windows import ResolvedWindow


def _close_bars(values, start="2021-01-04") -> pd.DataFrame:
    close = np.asarray(values, dtype=float)
    idx = pd.bdate_range(start, periods=len(close))
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1e6},
        index=idx,
    )


def test_breadth_math_hand_computed():
    a = _close_bars([10, 11, 12, 13, 14])
    b = _close_bars([10, 9, 8, 7, 6])
    c = _close_bars([5, 6, 20], start=a.index[2].strftime("%Y-%m-%d"))
    out = breadth_above_sma({"A": a, "B": b, "C": c}, length=3)

    assert out.iloc[:2].isna().all()
    assert out.iloc[2] == pytest.approx(0.5)
    assert out.iloc[3] == pytest.approx(0.5)
    assert out.iloc[4] == pytest.approx(2.0 / 3.0)


def test_breadth_excludes_short_history_and_empty_denom():
    short = _close_bars([1, 2, 3])
    assert breadth_above_sma({"S": short}, length=50).isna().all()
    long = _close_bars([10.0] * 60)
    rising = _close_bars(list(range(1, 61)))
    full = breadth_above_sma({"FLAT": long, "UP": rising}, length=50)
    only_up = breadth_above_sma({"FLAT": long, "UP": rising}, length=50, exclude={"FLAT"})
    assert full.iloc[-1] == pytest.approx(0.5)
    assert only_up.iloc[-1] == pytest.approx(1.0)


def test_breadth_no_lookahead_when_truncated():
    rng = np.random.default_rng(0)
    names = {f"T{i}": _close_bars(50 * np.exp(np.cumsum(rng.normal(0.0003, 0.02, 120)))) for i in range(6)}
    full = breadth_above_sma(names, length=50)
    for cut in (60, 80, 100, 118):
        part = {s: df.iloc[: cut + 1] for s, df in names.items()}
        truncated = breadth_above_sma(part, length=50)
        same = np.isclose(
            full.iloc[: cut + 1].to_numpy(dtype=float),
            truncated.to_numpy(dtype=float),
            equal_nan=True,
        )
        assert same.all(), f"breadth changed at cut={cut}"


def test_indicator_reads_injected_column_and_matches_helper():
    bars = {
        "SPY": _close_bars([100.0] * 80),
        "AAA": _close_bars(list(range(1, 81))),
        "BBB": _close_bars([50.0] * 80),
        "CCC": _close_bars(list(range(80, 0, -1))),
    }
    expected = breadth_above_sma({k: v for k, v in bars.items() if k != "SPY"}, length=50)
    for sym, df in bars.items():
        if sym == "SPY":
            continue
        frame = df.assign(
            BenchClose=bars["SPY"]["Close"].reindex(df.index),
            Breadth50=expected.reindex(df.index),
        )
        got = REGISTRY["breadth_above_sma50"](frame)
        assert got.index.equals(frame.index)
        assert np.allclose(got.to_numpy(), expected.reindex(frame.index).to_numpy(), equal_nan=True)
    assert expected.iloc[-1] == pytest.approx(1.0 / 3.0)
    frame = bars["AAA"].assign(
        BenchClose=bars["SPY"]["Close"], Breadth50=expected.reindex(bars["AAA"].index)
    )
    assert lookahead_violations(REGISTRY["breadth_above_sma50"], frame) == []


def test_engine_injects_same_breadth_on_every_ticker():
    n = 70
    bars = {
        "SPY": _close_bars([100.0] * n),
        "AAA": _close_bars(list(np.linspace(10, 40, n))),
        "BBB": _close_bars([20.0] * n),
    }
    window = ResolvedWindow("discovery", bars["SPY"].index[55].date(), bars["SPY"].index[65].date())
    spec = StrategySpec.model_validate({
        "id": "b", "entry": "breadth_above_sma50 > 0", "exit": "bars_held >= 2", "rank": "close",
        "indicators": {"breadth_above_sma50": {"fn": "breadth_above_sma50"}},
        "sizing": {"max_positions": 1, "max_weight": 0.2},
    })
    from quant_system.lab.data import bars_panel

    held = {s: bars[s] for s in ("AAA", "BBB")}
    breadth = breadth_above_sma(held, length=50)
    cal = bars_panel(held, "Close").index
    panels = indicator_panels(spec, held, bars["SPY"]["Close"], cal, breadth=breadth)
    a = panels["breadth_above_sma50"][:, 0]
    b = panels["breadth_above_sma50"][:, 1]
    assert np.allclose(a, b, equal_nan=True)
    assert a[69] == pytest.approx(0.5)
    res = run_spec(spec, bars, ["AAA", "BBB"], window, LabCostSettings(), breadth=breadth)
    assert not res.returns.empty


def test_theme_run_matches_full_universe_breadth(monkeypatch):
    """A theme-only bar set must get the same Breadth50 as a full-universe run.

    Theme {W,X} alone are both above SMA (breadth 1.0). Full universe adds
    flat Y and falling Z (breadth 2/4 = 0.5). After the mock loads the full
    lab_universe list, a theme-only bar set must still show 0.5.
    """
    clear_breadth_cache()
    n = 80
    # Theme {W,X} are both rising (above SMA) -> theme-only breadth 1.0.
    # Full adds flat Y and falling Z -> full breadth 2/4 = 0.5.
    full = {
        "W": _close_bars(list(np.linspace(10, 40, n))),  # above
        "X": _close_bars(list(np.linspace(5, 25, n))),   # above
        "Y": _close_bars([20.0] * n),                    # flat = not above
        "Z": _close_bars(list(np.linspace(40, 10, n))),  # below
    }
    theme = {"W": full["W"], "X": full["X"]}
    full_breadth = breadth_above_sma(full, length=50)
    theme_only = breadth_above_sma(theme, length=50)
    assert full_breadth.iloc[-1] == pytest.approx(0.5)
    assert theme_only.iloc[-1] == pytest.approx(1.0)
    assert full_breadth.iloc[-1] != theme_only.iloc[-1]

    def fake_symbols(name, **_kw):
        assert name == "broad"
        return list(full)

    def fake_load(symbols, **_kw):
        return {s: full[s].copy() for s in symbols if s in full}

    # lab_universe_breadth does late imports from these modules.
    monkeypatch.setattr("quant_system.lab.universe.universe_symbols", fake_symbols)
    monkeypatch.setattr("quant_system.lab.data.load_lab_bars", fake_load)

    spec = StrategySpec.model_validate({
        "id": "t", "entry": "breadth_above_sma50 > -1", "exit": "bars_held >= 1", "rank": "close",
        "indicators": {"breadth_above_sma50": {"fn": "breadth_above_sma50"}},
        "sizing": {"max_positions": 1, "max_weight": 0.2},
    })
    from quant_system.lab.data import bars_panel

    spy = _close_bars([100.0] * n)
    cal_theme = bars_panel(theme, "Close").index
    cal_full = bars_panel(full, "Close").index

    clear_breadth_cache()
    p_theme = indicator_panels(spec, theme, spy["Close"], cal_theme)
    clear_breadth_cache()
    p_full = indicator_panels(spec, full, spy["Close"], cal_full)

    theme_syms, full_syms = sorted(theme), sorted(full)
    for sym in theme_syms:
        t = p_theme["breadth_above_sma50"][:, theme_syms.index(sym)]
        f = p_full["breadth_above_sma50"][:, full_syms.index(sym)]
        assert np.allclose(t, f, equal_nan=True)
        assert t[-1] == pytest.approx(0.5)
        # Prove we did not accidentally use theme-only breadth (which would be 1.0).
        assert t[-1] != pytest.approx(1.0)
