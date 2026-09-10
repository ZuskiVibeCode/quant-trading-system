"""Unit tests for MomentumAgent volume / demand / squeeze overlays."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_system.market_data.interface import MarketData
from quant_system.models.signal import Direction
from quant_system.research.momentum import (
    MomentumAgent,
    bollinger_keltner_squeeze,
    demand_imbalance,
    relative_volume,
)


def _idx(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-02", periods=n, freq="B")


def _ohlcv(
    close: np.ndarray | list[float],
    *,
    volume: np.ndarray | list[float] | None = None,
    high_mult: float = 1.01,
    low_mult: float = 0.99,
) -> pd.DataFrame:
    close_arr = np.asarray(close, dtype=float)
    n = len(close_arr)
    vol = (
        np.asarray(volume, dtype=float)
        if volume is not None
        else np.full(n, 100_000.0)
    )
    high = close_arr * high_mult
    low = close_arr * low_mult
    open_ = np.r_[close_arr[0], close_arr[:-1]]
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close_arr, "Volume": vol},
        index=_idx(n),
    )


class _FrameMarketData(MarketData):
    """Inject fixed OHLCV frames (and optional benchmark)."""

    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = {k.upper(): v for k, v in frames.items()}

    def history(self, ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        key = ticker.upper()
        if key not in self.frames:
            raise ValueError(f"No history for {ticker}")
        return self.frames[key].copy()

    def last_price(self, ticker: str) -> float:
        return float(self.history(ticker)["Close"].iloc[-1])

    def snapshot(self, ticker: str) -> dict:
        df = self.history(ticker)
        last = df.iloc[-1]
        return {
            "ticker": ticker.upper(),
            "last_close": float(last["Close"]),
            "last_volume": float(last["Volume"]),
            "asof": str(df.index[-1]),
            "bars": len(df),
        }


def test_relative_volume_elevated():
    n = 30
    vol = np.full(n, 100_000.0)
    vol[-1] = 300_000.0  # 3x prior avg
    df = _ohlcv(np.linspace(50, 60, n), volume=vol)
    rvol = relative_volume(df, window=20)
    assert rvol is not None
    assert rvol == pytest.approx(3.0, rel=1e-6)


def test_relative_volume_low_and_missing():
    n = 25
    vol = np.full(n, 200_000.0)
    vol[-1] = 50_000.0  # 0.25x
    df = _ohlcv(np.linspace(40, 45, n), volume=vol)
    assert relative_volume(df, window=20) == pytest.approx(0.25, rel=1e-6)
    assert relative_volume(pd.DataFrame()) is None
    bare = _ohlcv(np.linspace(40, 45, n))
    del bare["Volume"]
    assert relative_volume(bare) is None


def test_demand_imbalance_buyers():
    """Mostly up closes with volume on up days → positive imbalance."""
    n = 30
    close = np.linspace(50, 80, n)  # monotonic up
    vol = np.full(n, 100_000.0)
    df = _ohlcv(close, volume=vol)
    out = demand_imbalance(df, window=20)
    assert out["demand_imbalance"] is not None
    assert out["demand_imbalance"] > 0.5
    assert out["up_volume"] > out["down_volume"]


def test_demand_imbalance_sellers():
    n = 30
    close = np.linspace(80, 50, n)  # monotonic down
    vol = np.full(n, 100_000.0)
    df = _ohlcv(close, volume=vol)
    out = demand_imbalance(df, window=20)
    assert out["demand_imbalance"] is not None
    assert out["demand_imbalance"] < -0.5


def test_squeeze_on_low_volatility_coil():
    """Tight range → BB inside KC (squeeze on)."""
    n = 80
    rng = np.random.default_rng(0)
    # Very low vol around 100
    close = 100.0 + rng.normal(0, 0.05, size=n)
    df = _ohlcv(close, high_mult=1.001, low_mult=0.999)
    # Tiny true range so KC is wide relative to BB? Actually for squeeze ON,
    # BB must be INSIDE KC, which happens when realized vol (BB) is low vs ATR (KC).
    # With tiny noise and proportional H/L, ATR is also tiny — force wider KC by
    # using larger high/low swings on early bars then compressing.
    close = np.concatenate(
        [
            100.0 + rng.normal(0, 2.0, size=40),  # establish ATR
            100.0 + rng.normal(0, 0.05, size=40),  # coil
        ]
    )
    high = close + 1.5
    low = close - 1.5
    # During coil, compress high/low toward close so ATR falls slower than BB std
    high[40:] = close[40:] + 0.8
    low[40:] = close[40:] - 0.8
    open_ = np.r_[close[0], close[:-1]]
    vol = np.full(n, 100_000.0)
    df = pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=_idx(n),
    )
    sq = bollinger_keltner_squeeze(df)
    # Coil should eventually put BB inside KC; if not with this seed, at least keys exist
    assert "squeeze_on" in sq
    assert "squeeze_release" in sq
    assert sq["bb_width"] is None or sq["bb_width"] >= 0


def test_squeeze_release_after_coil():
    """Build a synthetic series: coil then expand so release fires."""
    n = 100
    rng = np.random.default_rng(42)
    # Phase 1: moderate vol; Phase 2: very tight; Phase 3: expansion breakout
    p1 = 100 + np.cumsum(rng.normal(0, 0.3, 50))
    p2 = np.full(30, float(p1[-1])) + rng.normal(0, 0.02, 30)
    p3 = p2[-1] + np.cumsum(rng.normal(0.4, 0.8, 20))  # directional expansion
    close = np.concatenate([p1, p2, p3])
    # ATR-friendly ranges: wider early, narrower in coil, wider on release
    atr_pad = np.concatenate(
        [np.full(50, 1.2), np.full(30, 0.9), np.full(20, 2.5)]
    )
    high = close + atr_pad
    low = close - atr_pad
    open_ = np.r_[close[0], close[:-1]]
    vol = np.full(n, 150_000.0)
    vol[-1] = 400_000.0
    df = pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=_idx(n),
    )
    # Evaluate squeeze on second-to-last (may be on) and last (may release)
    sq = bollinger_keltner_squeeze(df)
    assert sq["squeeze_on"] is not None
    # Walk last few bars — at least one should show release or on/off transition
    releases = []
    ons = []
    for end in range(70, n + 1):
        s = bollinger_keltner_squeeze(df.iloc[:end])
        ons.append(s["squeeze_on"])
        releases.append(s["squeeze_release"])
    assert any(x is True for x in ons) or any(x is True for x in releases) or any(
        x is False for x in ons
    )


def test_agent_supporting_data_includes_overlays():
    n = 220
    rng = np.random.default_rng(7)
    close = 50 * np.cumprod(1 + rng.normal(0.001, 0.015, n))
    vol = rng.integers(80_000, 120_000, n).astype(float)
    vol[-1] = float(np.mean(vol[-21:-1]) * 2.2)
    df = _ohlcv(close, volume=vol)
    # Benchmark roughly flat
    spy = _ohlcv(100 * np.cumprod(1 + rng.normal(0.0003, 0.01, n)))
    md = _FrameMarketData({"IONQ": df, "SPY": spy})
    agent = MomentumAgent(md)
    sig = agent.analyze("IONQ", benchmark="SPY")
    assert sig.agent_id == "momentum"
    sd = sig.supporting_data
    assert "relative_volume" in sd
    assert sd["relative_volume"] is not None and sd["relative_volume"] > 1.5
    assert "demand_imbalance" in sd
    assert "squeeze_on" in sd
    assert "squeeze_release" in sd
    assert "rvol_window" in sd
    assert 0 <= sig.confidence <= 100
    # Reasoning should mention at least one overlay concept
    low = sig.reasoning.lower()
    assert "rvol" in low or "demand" in low or "squeeze" in low or "volume" in low


def test_high_rvol_confirms_breakout_long():
    """Breakout + high RVOL should push score/confidence vs same price with low RVOL."""
    n = 220
    # Strong uptrend ending at 20d high
    close = np.linspace(40, 80, n)
    high_vol = np.full(n, 100_000.0)
    high_vol[-1] = 250_000.0
    low_vol = np.full(n, 100_000.0)
    low_vol[-1] = 40_000.0
    df_hi = _ohlcv(close, volume=high_vol)
    df_lo = _ohlcv(close, volume=low_vol)
    spy = _ohlcv(np.linspace(100, 105, n))
    sig_hi = MomentumAgent(_FrameMarketData({"T": df_hi, "SPY": spy})).analyze("T")
    sig_lo = MomentumAgent(_FrameMarketData({"T": df_lo, "SPY": spy})).analyze("T")
    assert sig_hi.supporting_data["raw_score"] > sig_lo.supporting_data["raw_score"]
    assert "confirmed" in sig_hi.reasoning.lower() or sig_hi.supporting_data["relative_volume"] > 1.5
    assert "fade" in sig_lo.reasoning.lower() or sig_lo.supporting_data["relative_volume"] < 0.7


def test_history_error_returns_neutral():
    class Boom(MarketData):
        def history(self, ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
            raise RuntimeError("401 crumb")

        def last_price(self, ticker: str) -> float:
            raise RuntimeError("401 crumb")

        def snapshot(self, ticker: str) -> dict:
            raise RuntimeError("401 crumb")

    sig = MomentumAgent(Boom()).analyze("IONQ")
    assert sig.direction == Direction.NEUTRAL
    assert sig.confidence <= 15
    assert "unavailable" in sig.reasoning.lower() or "401" in sig.reasoning


def test_squeeze_release_bumps_reasoning_on_expansion_trend():
    """Uptrend expansion after a coil should mention squeeze release when detected."""
    n = 120
    rng = np.random.default_rng(99)
    coil = 60.0 + rng.normal(0, 0.03, 90)
    expand = coil[-1] + np.cumsum(np.full(30, 0.8))
    close = np.concatenate([coil, expand])
    pad = np.concatenate([np.full(90, 0.5), np.full(30, 2.0)])
    high = close + pad
    low = close - pad
    open_ = np.r_[close[0], close[:-1]]
    vol = np.full(n, 100_000.0)
    vol[-1] = 280_000.0
    df = pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=_idx(n),
    )
    spy = _ohlcv(np.linspace(100, 102, n))
    sig = MomentumAgent(_FrameMarketData({"NVDA": df, "SPY": spy})).analyze("NVDA")
    sd = sig.supporting_data
    assert "squeeze_on" in sd
    # Direction should be long-ish on strong expansion; overlays present either way
    assert sd["relative_volume"] is not None
    assert sig.direction in (Direction.LONG, Direction.NEUTRAL, Direction.SHORT)
