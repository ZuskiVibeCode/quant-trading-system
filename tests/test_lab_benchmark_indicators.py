"""Math checks for the SPY regime indicators (spy_trend_dist, spy_drawdown, spy_vol_ratio)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from quant_system.lab.indicators import REGISTRY


def _bench(values) -> pd.DataFrame:
    """A stock that never moves, with the given benchmark closes."""
    bench = pd.Series(values, index=pd.bdate_range("2022-01-03", periods=len(values)), dtype=float)
    flat = pd.Series(10.0, index=bench.index)
    return pd.DataFrame({"Open": flat, "High": flat, "Low": flat, "Close": flat, "Volume": 100.0,
                         "BenchClose": bench})


def test_spy_trend_dist():
    bars = _bench([100.0] * 49 + [150.0])
    out = REGISTRY["spy_trend_dist"](bars, length=50)
    assert out.iloc[:49].isna().all()                     # warm-up
    assert out.iloc[-1] == pytest.approx(150.0 / 101.0 - 1.0)  # SMA50 = (49*100 + 150) / 50 = 101
    # Ignores the stock's own close.
    assert REGISTRY["spy_trend_dist"](_bench([100.0] * 60), length=50).dropna().eq(0.0).all()


def test_spy_drawdown():
    vals = [100.0] * 70
    vals[5] = 200.0
    vals[69] = 150.0
    out = REGISTRY["spy_drawdown"](_bench(vals), length=60)
    assert out.iloc[:59].isna().all()
    assert out.iloc[59] == pytest.approx(-0.5)   # window 0..59 holds the 200 peak
    assert out.iloc[64] == pytest.approx(-0.5)   # window 5..64: peak still inside
    assert out.iloc[65] == pytest.approx(0.0)    # window 6..65: peak rolled off, max 100
    assert out.iloc[69] == pytest.approx(0.0)    # new high today
    assert out.dropna().between(-1.0, 0.0).all()


def _from_returns(rets) -> list[float]:
    return list(100.0 * np.cumprod(np.concatenate([[1.0], 1.0 + np.asarray(rets)])))


def test_spy_vol_ratio():
    calm = [0.01, -0.01] * 15            # 30 returns of +-1%
    hot = [0.02, -0.02] * 5              # then 10 returns of +-2%
    bars = _bench(_from_returns(calm + hot))
    out = REGISTRY["spy_vol_ratio"](bars, fast=10, slow=30)
    assert out.iloc[:30].isna().all()     # bar 0 has no return; slow needs 30 returns
    # Bar 30: last 30 returns all +-1% (mean 0): sample var 30e-4/29; fast: 10e-4/9.
    assert out.iloc[30] == pytest.approx(math.sqrt((10e-4 / 9) / (30e-4 / 29)))
    # Last bar: fast = ten +-2% (var 40e-4/9); slow = twenty +-1% and ten +-2% (var 60e-4/29).
    assert out.iloc[-1] == pytest.approx(math.sqrt((40e-4 / 9) / (60e-4 / 29)))
    # Flat SPY: zero slow stdev -> undefined, not inf.
    assert REGISTRY["spy_vol_ratio"](_bench([100.0] * 50), fast=5, slow=30).isna().all()
