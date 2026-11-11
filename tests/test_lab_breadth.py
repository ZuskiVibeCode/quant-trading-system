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


from quant_system.lab.engine import net_new_highs as net_new_highs_frac


def test_breadth200_math_and_warmup():
    # Reuse breadth_above_sma with length=200; short names excluded from both counts.
    up = _close_bars(list(range(1, 221)))          # 220 bars, above SMA once eligible
    down = _close_bars(list(range(220, 0, -1)))    # below SMA
    short = _close_bars(list(range(1, 51)))        # 50 bars: never eligible at 200
    out = breadth_above_sma({"UP": up, "DOWN": down, "SHORT": short}, length=200)
    assert out.iloc[:199].isna().all()
    assert out.iloc[199] == pytest.approx(0.5)  # UP+DOWN eligible; SHORT out of denom
    # 3-bar hand check (same helper, different length) still holds
    a, b = _close_bars([10, 11, 12, 13]), _close_bars([10, 9, 8, 7])
    short3 = _close_bars([1, 2])
    h = breadth_above_sma({"A": a, "B": b, "S": short3}, length=3)
    assert h.iloc[-1] == pytest.approx(0.5)


def test_net_new_highs_hand_computed():
    # length=4. Shared calendar of 6 bars.
    # H: 1,2,3,4,5,6  always a new high once eligible
    # L: 6,5,4,3,2,1  always a new low
    # F: 9,9,9,9,9,9  flat (max==min) -> neither
    # M: 4,3,2,1,8,7  mixed: bar3 window 4,3,2,1 close=1=min -> low;
    #                 bar4 window 3,2,1,8 close=8=max -> high;
    #                 bar5 window 2,1,8,7 close=7 neither
    # S: last 3 bars only -> not eligible (needs 4)
    h = _close_bars([1, 2, 3, 4, 5, 6])
    lo = _close_bars([6, 5, 4, 3, 2, 1])
    flat = _close_bars([9, 9, 9, 9, 9, 9])
    mixed = _close_bars([4, 3, 2, 1, 8, 7])
    short = _close_bars([10, 11, 12], start=h.index[3].strftime("%Y-%m-%d"))
    out = net_new_highs_frac({"H": h, "L": lo, "F": flat, "M": mixed, "S": short}, length=4)
    assert out.iloc[:3].isna().all()
    # bar 3: H high, L low, F neither, M low, S not eligible -> (1-2)/4 = -0.25
    assert out.iloc[3] == pytest.approx(-0.25)
    # bar 4: H high, L low, F neither, M high, S not eligible -> (2-1)/4 = 0.25
    assert out.iloc[4] == pytest.approx(0.25)
    # bar 5: H high, L low, F neither, M neither, S not eligible (only 3 own bars) -> (1-1)/4 = 0
    assert out.iloc[5] == pytest.approx(0.0)


def test_net_new_highs_warmup_flat_and_empty_denom():
    short = _close_bars([1, 2, 3])
    assert net_new_highs_frac({"S": short}, length=252).isna().all()
    flat = _close_bars([10.0] * 260)
    out = net_new_highs_frac({"F": flat}, length=252)
    assert out.iloc[:251].isna().all()
    assert (out.dropna() == 0.0).all()  # eligible but neither high nor low
    # Two flats still denom>0 and value 0, not NaN
    assert net_new_highs_frac({"A": flat, "B": flat}, length=252).iloc[-1] == pytest.approx(0.0)


def test_market_wide_no_lookahead_when_truncated():
    rng = np.random.default_rng(1)
    names = {f"T{i}": _close_bars(50 * np.exp(np.cumsum(rng.normal(0.0003, 0.02, 280)))) for i in range(6)}
    b200 = breadth_above_sma(names, length=200)
    nnh = net_new_highs_frac(names, length=252)
    for cut in (210, 250, 260, 278):
        part = {s: df.iloc[: cut + 1] for s, df in names.items()}
        for full, fn, length in (
            (b200, breadth_above_sma, 200),
            (nnh, net_new_highs_frac, 252),
        ):
            trunc = fn(part, length=length)
            same = np.isclose(
                full.iloc[: cut + 1].to_numpy(dtype=float),
                trunc.to_numpy(dtype=float),
                equal_nan=True,
            )
            assert same.all(), f"{fn.__name__} changed at cut={cut}"


def test_indicators_read_injected_columns():
    n = 260
    spy = _close_bars([100.0] * n)
    names = {
        "UP": _close_bars(list(range(1, n + 1))),
        "DOWN": _close_bars(list(range(n, 0, -1))),
        "FLAT": _close_bars([50.0] * n),
    }
    b50 = breadth_above_sma(names, length=50)
    b200 = breadth_above_sma(names, length=200)
    nnh = net_new_highs_frac(names, length=252)
    frame = names["UP"].assign(
        BenchClose=spy["Close"].reindex(names["UP"].index),
        Breadth50=b50.reindex(names["UP"].index),
        Breadth200=b200.reindex(names["UP"].index),
        NetNewHighs252=nnh.reindex(names["UP"].index),
    )
    assert np.allclose(REGISTRY["breadth_above_sma50"](frame), b50, equal_nan=True)
    assert np.allclose(REGISTRY["breadth_above_sma200"](frame), b200, equal_nan=True)
    assert np.allclose(REGISTRY["net_new_highs"](frame), nnh, equal_nan=True)
    # Eligible: all three; UP above 200-SMA, DOWN+FLAT not -> 1/3
    assert b200.iloc[-1] == pytest.approx(1.0 / 3.0)
    # UP is a new high, DOWN a new low, FLAT neither -> 0
    assert nnh.iloc[-1] == pytest.approx(0.0)
    assert lookahead_violations(REGISTRY["breadth_above_sma200"], frame) == []
    assert lookahead_violations(REGISTRY["net_new_highs"], frame) == []


def test_breadth50_unchanged_when_new_columns_present():
    """Injecting Breadth200 / NetNewHighs252 must not change breadth_above_sma50."""
    n = 80
    names = {
        "AAA": _close_bars(list(np.linspace(10, 40, n))),
        "BBB": _close_bars([20.0] * n),
    }
    b50 = breadth_above_sma(names, length=50)
    spy = _close_bars([100.0] * n)
    # Junk values in the new columns should be ignored by the 50-day indicator.
    frame = names["AAA"].assign(
        BenchClose=spy["Close"],
        Breadth50=b50.reindex(names["AAA"].index),
        Breadth200=pd.Series(0.99, index=names["AAA"].index),
        NetNewHighs252=pd.Series(-0.99, index=names["AAA"].index),
    )
    got = REGISTRY["breadth_above_sma50"](frame)
    assert np.allclose(got, b50, equal_nan=True)
    assert got.iloc[-1] == pytest.approx(0.5)


def test_theme_run_matches_full_universe_for_new_series(monkeypatch):
    """Theme-only bars get the same Breadth200 and NetNewHighs252 as a full-universe run."""
    clear_breadth_cache()
    n = 260
    full = {
        "W": _close_bars(list(np.linspace(10, 40, n))),  # above SMA, new high
        "X": _close_bars(list(np.linspace(5, 25, n))),   # above SMA, new high
        "Y": _close_bars([20.0] * n),                    # flat: not above, neither high/low
        "Z": _close_bars(list(np.linspace(40, 10, n))),  # below SMA, new low
    }
    theme = {"W": full["W"], "X": full["X"]}
    full_b200 = breadth_above_sma(full, length=200)
    theme_b200 = breadth_above_sma(theme, length=200)
    full_nnh = net_new_highs_frac(full, length=252)
    theme_nnh = net_new_highs_frac(theme, length=252)
    # Theme-only would be 2/2=1.0 for both (two new highs, both above SMA).
    # Full: breadth 2/4=0.5; nnh (2 highs - 1 low)/4 = 0.25.
    assert full_b200.iloc[-1] == pytest.approx(0.5)
    assert theme_b200.iloc[-1] == pytest.approx(1.0)
    assert full_nnh.iloc[-1] == pytest.approx(0.25)
    assert theme_nnh.iloc[-1] == pytest.approx(1.0)

    def fake_symbols(name, **_kw):
        assert name == "broad"
        return list(full)

    def fake_load(symbols, **_kw):
        return {s: full[s].copy() for s in symbols if s in full}

    monkeypatch.setattr("quant_system.lab.universe.universe_symbols", fake_symbols)
    monkeypatch.setattr("quant_system.lab.data.load_lab_bars", fake_load)

    spec = StrategySpec.model_validate({
        "id": "t", "entry": "breadth_above_sma200 > -1", "exit": "bars_held >= 1", "rank": "close",
        "indicators": {
            "breadth_above_sma50": {"fn": "breadth_above_sma50"},
            "breadth_above_sma200": {"fn": "breadth_above_sma200"},
            "net_new_highs": {"fn": "net_new_highs"},
        },
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
    for ind, expected in (("breadth_above_sma200", 0.5), ("net_new_highs", 0.25), ("breadth_above_sma50", 0.5)):
        for sym in theme_syms:
            t = p_theme[ind][:, theme_syms.index(sym)]
            f = p_full[ind][:, full_syms.index(sym)]
            assert np.allclose(t, f, equal_nan=True)
            assert t[-1] == pytest.approx(expected)
            if ind != "breadth_above_sma50":
                assert t[-1] != pytest.approx(1.0)
