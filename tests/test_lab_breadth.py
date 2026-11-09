"""Math and no-lookahead checks for breadth_above_sma50 / engine Breadth50 injection."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_system.lab.engine import breadth_above_sma, indicator_panels, run_spec
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
    # length=3 for a short hand check. Three names on a shared calendar.
    # A: 10,11,12,13,14  -> SMA3 at bar2=11, bar3=12, bar4=13; above when close>sma: 12>11,13>12,14>13 -> T,T,T
    # B: 10, 9, 8, 7, 6  -> SMA3: bar2=9, bar3=8, bar4=7; above: 8>9 F, 7>8 F, 6>7 F
    # C: only 3 bars starting late (aligned to last 3 of A): 5,6,20
    #    on shared index C starts at bar 2: values NaN,NaN,5,6,20
    #    C's own bars: 5,6,20 -> eligible only at its 3rd bar (index 4): SMA=10.333, 20>10.33 T
    a = _close_bars([10, 11, 12, 13, 14])
    b = _close_bars([10, 9, 8, 7, 6])
    c = _close_bars([5, 6, 20], start=a.index[2].strftime("%Y-%m-%d"))
    out = breadth_above_sma({"A": a, "B": b, "C": c}, length=3)

    assert out.iloc[:2].isna().all()  # no name has 3 bars yet
    # bar 2: A and B eligible (2/2 above? A yes B no) -> 0.5; C has only 1 own bar
    assert out.iloc[2] == pytest.approx(0.5)
    # bar 3: A,B eligible -> 0.5; C has 2 own bars
    assert out.iloc[3] == pytest.approx(0.5)
    # bar 4: A,B,C eligible; A above, B below, C above -> 2/3
    assert out.iloc[4] == pytest.approx(2.0 / 3.0)


def test_breadth_excludes_short_history_and_empty_denom():
    short = _close_bars([1, 2, 3])  # only 3 bars, length=50 -> never eligible
    assert breadth_above_sma({"S": short}, length=50).isna().all()
    # exclude drops a name that would otherwise count
    long = _close_bars([10.0] * 60)
    rising = _close_bars(list(range(1, 61)))
    full = breadth_above_sma({"FLAT": long, "UP": rising}, length=50)
    only_up = breadth_above_sma({"FLAT": long, "UP": rising}, length=50, exclude={"FLAT"})
    assert full.iloc[-1] == pytest.approx(0.5)  # flat: close==sma so NOT above; up is above
    assert only_up.iloc[-1] == pytest.approx(1.0)


def test_breadth_no_lookahead_when_truncated():
    rng = np.random.default_rng(0)
    names = {f"T{i}": _close_bars(50 * np.exp(np.cumsum(rng.normal(0.0003, 0.02, 120)))) for i in range(6)}
    full = breadth_above_sma(names, length=50)
    for cut in (60, 80, 100, 118):
        part = {s: df.iloc[: cut + 1] for s, df in names.items()}
        truncated = breadth_above_sma(part, length=50)
        a, b = full.iloc[: cut + 1], truncated
        same = np.isclose(a.to_numpy(dtype=float), b.to_numpy(dtype=float), equal_nan=True)
        assert same.all(), f"breadth changed at cut={cut}"


def test_indicator_reads_injected_column_and_matches_helper():
    bars = {
        "SPY": _close_bars([100.0] * 80),
        "AAA": _close_bars(list(range(1, 81))),
        "BBB": _close_bars([50.0] * 80),  # flat: close == SMA, not above
        "CCC": _close_bars(list(range(80, 0, -1))),  # falling: below SMA
    }
    expected = breadth_above_sma({k: v for k, v in bars.items() if k != "SPY"}, length=50)
    # Mirror indicator_panels injection
    for sym, df in list(bars.items()):
        if sym == "SPY":
            continue
        frame = df.assign(BenchClose=bars["SPY"]["Close"].reindex(df.index), Breadth50=expected.reindex(df.index))
        got = REGISTRY["breadth_above_sma50"](frame)
        assert got.index.equals(frame.index)
        assert np.allclose(got.to_numpy(), expected.reindex(frame.index).to_numpy(), equal_nan=True)
        # Every ticker gets the same series on shared dates
        assert got.iloc[-1] == pytest.approx(expected.iloc[-1])
    assert expected.iloc[-1] == pytest.approx(1.0 / 3.0)  # only AAA above
    # Generic lookahead on a frame that already carries Breadth50
    frame = bars["AAA"].assign(BenchClose=bars["SPY"]["Close"], Breadth50=expected.reindex(bars["AAA"].index))
    assert lookahead_violations(REGISTRY["breadth_above_sma50"], frame) == []


def test_engine_injects_same_breadth_on_every_ticker():
    # Rising AAA above SMA; flat BBB at SMA (not above); need 50+ bars.
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
    # Direct panel check: both names see identical Breadth50 values
    from quant_system.lab.data import bars_panel
    held = {s: bars[s] for s in ("AAA", "BBB")}
    cal = bars_panel(held, "Close").index
    panels = indicator_panels(spec, held, bars["SPY"]["Close"], cal)
    a = panels["breadth_above_sma50"][:, 0]
    b = panels["breadth_above_sma50"][:, 1]
    assert np.allclose(a, b, equal_nan=True)
    # Hand check last warm bar: 1 of 2 names above -> 0.5
    assert a[69] == pytest.approx(0.5)
    res = run_spec(spec, bars, ["AAA", "BBB"], window, LabCostSettings())
    assert not res.returns.empty
