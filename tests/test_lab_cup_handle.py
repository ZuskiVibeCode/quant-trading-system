"""Math checks for the cup_handle pattern detector on synthetic bars."""

from __future__ import annotations

import numpy as np
import pandas as pd

from quant_system.lab.indicators import REGISTRY, lookahead_violations

CH = REGISTRY["cup_handle"]


def _bars(path, volume) -> pd.DataFrame:
    close = np.asarray(path, dtype=float)
    idx = pd.bdate_range("2020-01-02", periods=len(close))
    return pd.DataFrame(
        {"Open": close, "High": close * 1.002, "Low": close * 0.998, "Close": close,
         "Volume": np.asarray(volume, dtype=float), "BenchClose": close},
        index=idx,
    )


def _pattern(cup_depth=0.25, v_shape=False, handle_drop=0.06, breakout_vol=3.0, cup_len=100):
    """60-bar uptrend 50 -> 100, cup of cup_len bars back to 99, a 10-bar handle, breakout, 5 quiet bars."""
    up = list(np.linspace(50, 100, 61))
    k = np.arange(1, cup_len + 1) / cup_len
    shape = (1 - np.abs(2 * k - 1)) if v_shape else np.sin(np.pi * k)
    cup = list(100 - 100 * cup_depth * shape - k)  # right rim ~99
    rim = cup[-1]
    hl = rim * (1 - handle_drop)
    handle = list(np.linspace(rim, hl, 6)[1:]) + list(np.linspace(hl, rim * 0.99, 6)[1:])
    breakout = [rim * 1.03]
    after = [rim * 1.02] * 5
    path = up + cup + handle + breakout + after
    vol = [1000.0] * len(path)
    for i in range(len(up) + len(cup), len(up) + len(cup) + len(handle)):
        vol[i] = 700.0  # handle on falling volume
    b_idx = len(up) + len(cup) + len(handle)
    vol[b_idx] = 1000.0 * breakout_vol
    return _bars(path, vol), b_idx


def test_fires_on_breakout_bar_only():
    bars, b = _pattern()
    sig = CH(bars, require_falling_volume=1)
    assert sig.iloc[b] == 1.0
    assert sig.drop(sig.index[b]).eq(0.0).all()
    assert lookahead_violations(CH, bars, cutoffs=[b - 1, b, b + 2]) == []
    # Truncated right before the breakout bar: no signal yet.
    assert CH(bars.iloc[:b]).eq(0.0).all()


def test_rejects_v_shape():
    bars, b = _pattern(v_shape=True)
    assert CH(bars).iloc[b] == 0.0
    # Same V passes once the bottom-share rule is switched off: that rule is what rejects it.
    assert CH(bars, min_bottom_share=0.0).iloc[b] == 1.0


def test_rejects_too_deep_cup():
    bars, b = _pattern(cup_depth=0.45, handle_drop=0.05)
    assert CH(bars).iloc[b] == 0.0
    assert CH(bars, max_cup_depth=0.5).iloc[b] == 1.0


def test_rejects_too_deep_handle():
    bars, b = _pattern(handle_drop=0.14)  # > 12% handle depth (and > 1/3 of a 25% cup)
    assert CH(bars).iloc[b] == 0.0
    # Within max_handle_depth but more than 1/3 of the cup's depth.
    bars, b = _pattern(cup_depth=0.21, handle_drop=0.09)
    assert CH(bars).iloc[b] == 0.0
    assert CH(bars, max_handle_cup_frac=1.0).iloc[b] == 1.0


def test_rejects_handle_in_lower_half():
    bars, b = _pattern(cup_depth=0.20, handle_drop=0.14)  # handle low dips below the cup's midpoint
    loose = dict(max_handle_depth=0.5, max_handle_cup_frac=1.0)
    assert CH(bars, **loose).iloc[b] == 0.0
    assert CH(bars, **loose, handle_min_position=0.0).iloc[b] == 1.0


def test_rejects_breakout_without_volume():
    bars, b = _pattern(breakout_vol=1.0)
    assert CH(bars).iloc[b] == 0.0
    assert CH(bars, breakout_volume_mult=1.0).iloc[b] == 1.0


def test_rejects_without_prior_uptrend():
    bars, b = _pattern()
    flat_start = bars.copy()
    for col in ("Open", "High", "Low", "Close"):
        flat_start.iloc[:61, flat_start.columns.get_loc(col)] = bars[col].iloc[60] * np.linspace(0.9, 1.0, 61)
    assert CH(flat_start).iloc[b] == 0.0


STAGE = REGISTRY["cup_handle_stage"]


def test_stage_walks_through_every_stage():
    bars, b = _pattern()  # left rim bar 60 (confirmed at 65), cup low ~110, right rim 160, handle 161-170
    st = STAGE(bars).to_numpy()
    assert set(np.unique(st)) == {0.0, 1.0, 2.0, 3.0, 4.0, 5.0}
    assert (st[:65] == 0).all()           # uptrend, left rim not yet confirmed
    assert (st[65:80] == 1).all()          # rim set, cup still shallow
    assert (st[100:140] == 2).all()        # 12-33% drawdown, rounded bottom
    assert (st[153:160] == 3).all()        # back within rim_tolerance, right rim unconfirmed
    assert (st[166:b] == 4).all()          # handle after the confirmed right-rim pivot
    assert st[b] == 5 and (st[b + 1 : b + 5] == 0).all()  # pattern complete after breakout
    assert lookahead_violations(STAGE, bars, cutoffs=[70, 120, 155, 168, b, b + 3]) == []


def test_stage_5_matches_cup_handle():
    for kw in ({}, {"v_shape": True}, {"breakout_vol": 1.0}, {"handle_drop": 0.14}):
        bars, _ = _pattern(**kw)
        for settings in ({}, {"require_falling_volume": 1}, {"min_bottom_share": 0.0}):
            assert ((STAGE(bars, **settings) == 5) == (CH(bars, **settings) == 1)).all()


def test_stage_v_shape_and_failed_handle():
    bars, b = _pattern(v_shape=True)
    st = STAGE(bars).to_numpy()
    assert st.max() <= 2 and (st[140:b + 1] == 1).all()  # never a valid cup or handle
    bars, b = _pattern(handle_drop=0.14)  # handle too deep: never reaches 4 or 5 (cup itself stays 3)
    st = STAGE(bars).to_numpy()
    assert st[153:b + 1].max() == 3


def test_stage_dies_on_too_deep_cup():
    bars, b = _pattern(cup_depth=0.45, handle_drop=0.05)
    st = STAGE(bars).to_numpy()
    assert st[120:160].max() == 0  # the only candidate drew down past 33%
    assert st[160:b + 1].max() == 1  # the old right rim is merely a new left-rim candidate
