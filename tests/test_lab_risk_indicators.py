"""Math checks for beta, realized_vol and vol_vs_spy."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from quant_system.lab.indicators import REGISTRY


def _prices(rets) -> np.ndarray:
    return 100.0 * np.cumprod(np.concatenate([[1.0], 1.0 + np.asarray(rets, dtype=float)]))


def _bars(stock_rets, spy_rets) -> pd.DataFrame:
    close, bench = _prices(stock_rets), _prices(spy_rets)
    idx = pd.bdate_range("2023-01-02", periods=len(close))
    return pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": 100.0,
                         "BenchClose": bench}, index=idx)


ALT = [0.01, -0.01] * 30  # 60 returns of +-1%: mean 0, sample var 60e-4/59


def test_beta():
    # Stock moves 2x SPY every day: beta exactly 2; moving -0.5x: beta -0.5.
    out = REGISTRY["beta"](_bars([2 * r for r in ALT], ALT), length=60)
    assert out.iloc[:60].isna().all() and out.iloc[60] == pytest.approx(2.0)
    assert REGISTRY["beta"](_bars([-0.5 * r for r in ALT], ALT), length=60).iloc[-1] == pytest.approx(-0.5)
    # Hand-computed: 20 SPY returns +-1%; stock = SPY on the first 10, flat on the last 10.
    # cov = (10 x 1e-4) / 19, var = (20 x 1e-4) / 19 -> beta 0.5.
    spy = [0.01, -0.01] * 10
    stock = spy[:10] + [0.0] * 10
    assert REGISTRY["beta"](_bars(stock, spy), length=20).iloc[-1] == pytest.approx(0.5)
    # Flat SPY: zero variance -> NaN.
    assert REGISTRY["beta"](_bars(ALT, [0.0] * 60), length=20).isna().all()


def test_realized_vol():
    out = REGISTRY["realized_vol"](_bars(ALT, ALT), length=20)
    assert out.iloc[:20].isna().all()  # bar 0 has no return
    assert out.iloc[20] == pytest.approx(math.sqrt(20e-4 / 19))  # not annualized
    # Returns 1%, 2%, 3%, 4%, 5%: mean 3%, sample var = (4+1+0+1+4)e-4 / 4 = 2.5e-4.
    assert REGISTRY["realized_vol"](_bars([0.01, 0.02, 0.03, 0.04, 0.05], [0.0] * 5), length=5).iloc[-1] == (
        pytest.approx(math.sqrt(2.5e-4)))
    assert REGISTRY["realized_vol"](_bars([0.0] * 30, ALT[:30]), length=5).dropna().eq(0.0).all()


def test_vol_vs_spy():
    out = REGISTRY["vol_vs_spy"](_bars([3 * r for r in ALT], ALT), length=60)
    assert out.iloc[:60].isna().all() and out.iloc[60] == pytest.approx(3.0)
    # Stock +-2% vs SPY +-1% over 20 bars: sqrt(80e-4/19) / sqrt(20e-4/19) = 2.
    assert REGISTRY["vol_vs_spy"](_bars([0.02, -0.02] * 10, [0.01, -0.01] * 10), length=20).iloc[-1] == (
        pytest.approx(2.0))
    assert REGISTRY["vol_vs_spy"](_bars(ALT, [0.0] * 60), length=20).isna().all()  # flat SPY
