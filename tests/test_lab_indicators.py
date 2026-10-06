"""Lab indicators: one generic lookahead test for every registered indicator, plus math checks."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_system.lab.indicators import REGISTRY, get_indicator, lookahead_violations
from quant_system.lab.indicators.registry import IndicatorDef, ParamDef, indicator


def _random_bars(n: int = 600, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2016-01-04", periods=n)
    close = 50 * np.exp(np.cumsum(rng.normal(0.0003, 0.02, n)))
    open_ = close * np.exp(rng.normal(0, 0.005, n))
    bench = 200 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n)))
    return pd.DataFrame(
        {
            "Open": open_,
            "High": np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, n)),
            "Low": np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, n)),
            "Close": close,
            "Volume": rng.uniform(1e5, 1e6, n),
            "BenchClose": bench,
        },
        index=idx,
    )


def _settings_variants(ind: IndicatorDef) -> list[dict]:
    """Defaults plus every setting pushed toward the top of its range."""
    out = [{}]
    for key, p in ind.params.items():
        val = p.default * 1.5
        val = min(int(round(val)) if p.is_int else val, p.hi)
        out.append({key: val})
    return out


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_every_registered_indicator_has_no_lookahead(name):
    ind = REGISTRY[name]
    bars = _random_bars()
    for settings in _settings_variants(ind):
        assert lookahead_violations(ind, bars, params=settings, cutoffs=[30, 150, 333, 598]) == []
        out = ind(bars, **settings)
        assert out.index.equals(bars.index)
        assert out.iloc[-50:].notna().all(), f"{name} still warming up after 550 bars"


def test_lookahead_check_catches_a_cheating_indicator():
    peek = IndicatorDef("peek", lambda b, length: b["Close"].shift(-length), {"length": ParamDef(1, 1, 5)}, "")
    problems = lookahead_violations(peek, _random_bars(100))
    assert problems and "changes when bars after" in problems[0]

    centered = IndicatorDef(
        "centered", lambda b, length: b["Close"].rolling(length, center=True).mean(),
        {"length": ParamDef(5, 2, 9)}, "",
    )
    assert lookahead_violations(centered, _random_bars(100))

    def mutate(b, length):
        b["Close"] = b["Close"] * 2
        return b["Close"].rolling(length).mean()

    assert any("modified its input" in p for p in lookahead_violations(
        IndicatorDef("mutate", mutate, {"length": ParamDef(5, 2, 9)}, ""), _random_bars(100)))


def test_settings_are_coerced_and_checked():
    sma = get_indicator("sma")
    assert sma.coerce({"length": 19.6}) == {"length": 20}
    assert get_indicator("bb_pctb").coerce({}) == {"length": 20, "k": 2.0}
    with pytest.raises(ValueError, match="unknown setting"):
        sma.coerce({"window": 5})
    with pytest.raises(ValueError, match="outside"):
        sma.coerce({"length": 1000})
    with pytest.raises(ValueError, match="must be a number"):
        sma.coerce({"length": "20"})
    with pytest.raises(ValueError, match="unknown indicator"):
        get_indicator("vwap_magic")
    with pytest.raises(ValueError, match="already registered"):
        indicator("sma", length=(5, 2, 9))(lambda b, length: b["Close"])


def _line(values, **extra) -> pd.DataFrame:
    close = pd.Series(values, index=pd.bdate_range("2021-01-04", periods=len(values)), dtype=float)
    cols = {"Open": close, "High": close, "Low": close, "Close": close, "Volume": 100.0, "BenchClose": close}
    cols.update(extra)
    return pd.DataFrame(cols, index=close.index)


def test_indicator_math():
    up = _line(np.arange(1, 41))
    assert REGISTRY["sma"](up, length=4).iloc[3] == pytest.approx(2.5)
    assert REGISTRY["sma"](up, length=4).iloc[:3].isna().all()
    assert REGISTRY["rsi"](up, length=14).iloc[-1] == 100.0
    assert REGISTRY["rsi"](_line(np.arange(40, 0, -1)), length=14).iloc[-1] == pytest.approx(0.0)
    assert REGISTRY["roc"](up, length=1).iloc[1] == pytest.approx(1.0)
    assert REGISTRY["rel_strength"](up, length=5).dropna().abs().max() == pytest.approx(0.0)
    assert REGISTRY["rel_volume"](up, length=5).dropna().eq(1.0).all()
    # Donchian excludes today: a new high is above the prior-20 high.
    dh = REGISTRY["donchian_high"](up, length=20)
    assert np.isnan(dh.iloc[19]) and dh.iloc[20] == 20.0 and up["Close"].iloc[20] > dh.iloc[20]
    flat = _line([10.0] * 60)
    assert REGISTRY["macd"](flat).dropna().eq(0.0).all()
    assert REGISTRY["zscore"](flat, length=5).dropna().empty  # zero spread -> undefined
    wavy = _line(10 + np.sin(np.arange(60)))
    assert REGISTRY["bb_pctb"](wavy, length=20).dropna().between(-0.5, 1.5).all()
    # Constant 2-point range, no gaps: true range is 2 every bar, so ATR is 2.
    ranged = _line([10.0] * 40, High=11.0, Low=9.0)
    assert REGISTRY["atr"](ranged, length=14).dropna().eq(2.0).all()
    assert REGISTRY["atr_pct"](ranged, length=14).iloc[-1] == pytest.approx(0.2)
    assert REGISTRY["ema"](up, length=3).iloc[2] == pytest.approx(2.25)  # 1, 1.5, 2.25 (alpha 0.5)
