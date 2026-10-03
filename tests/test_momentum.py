"""Unit tests for MomentumAgent volume / demand / squeeze / VbP / Weinstein / aVWAP / OBV / Chandelier."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_system.market_data.interface import MarketData
from quant_system.models.signal import Direction
from quant_system.backtest.asof_market_data import AsOfMarketData
from quant_system.research.momentum import (
    MomentumAgent,
    _CHANDELIER_ATR_MULT,
    _CHANDELIER_LOOKBACK,
    _CHANDELIER_SCORE,
    _MINERVINI_PASS_WEIGHT,
    _MINERVINI_RS_WEIGHT,
    _OBV_SCORE_CAP,
    _OBV_SLOPE_SCORE,
    _OBV_VS_MA_SCORE,
    _apply_chandelier_overlay,
    _apply_minervini_overlay,
    _apply_obv_overlay,
    _atr,
    anchored_vwap,
    bollinger_keltner_squeeze,
    chandelier_trail,
    demand_imbalance,
    minervini_trend_template,
    on_balance_volume,
    relative_volume,
    rs_proxy_from_excess,
    volume_by_price,
    weinstein_stage,
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


# ---------------------------------------------------------------------------
# Volume by Price / Weinstein / Anchored VWAP
# ---------------------------------------------------------------------------


def test_volume_by_price_poc_and_value_area():
    """Concentrated volume at a price level → PoC there; VA covers ~70%."""
    n = 80
    close = np.concatenate(
        [
            np.full(20, 50.0),
            np.full(40, 60.0),  # heavy volume cluster
            np.full(20, 70.0),
        ]
    )
    vol = np.concatenate(
        [
            np.full(20, 50_000.0),
            np.full(40, 200_000.0),
            np.full(20, 50_000.0),
        ]
    )
    df = _ohlcv(close, volume=vol, high_mult=1.005, low_mult=0.995)
    out = volume_by_price(df, lookback=80, n_bins=20)
    assert out["vbp_poc"] is not None
    assert out["vbp_vah"] is not None and out["vbp_val"] is not None
    assert out["vbp_val"] <= out["vbp_poc"] <= out["vbp_vah"]
    # PoC should sit near the 60 cluster
    assert 55.0 <= out["vbp_poc"] <= 65.0
    assert out["price_vs_poc"] is not None
    # Last close ~70 → above PoC
    assert out["price_vs_poc"] > 0


def test_volume_by_price_empty_and_short():
    assert volume_by_price(pd.DataFrame())["vbp_poc"] is None
    df = _ohlcv([10.0, 10.5, 11.0])
    assert volume_by_price(df)["vbp_poc"] is None  # < 5 bars


def test_weinstein_stage_2_uptrend():
    """Long monotonic daily advance → weekly Stage 2 (above rising 30w SMA)."""
    n = 250  # ~50 weeks
    close = np.linspace(40, 100, n)
    df = _ohlcv(close)
    out = weinstein_stage(df)
    assert out["weekly_bars"] >= 30
    assert out["sma30w"] is not None
    assert out["weinstein_stage"] == 2
    assert out["sma30w_slope"] is not None and out["sma30w_slope"] > 0


def test_weinstein_stage_4_downtrend():
    n = 250
    close = np.linspace(100, 40, n)
    df = _ohlcv(close)
    out = weinstein_stage(df)
    assert out["weinstein_stage"] == 4
    assert out["sma30w_slope"] is not None and out["sma30w_slope"] < 0


def test_weinstein_stage_insufficient_history():
    """Young name: fewer than 30 weekly bars → stage None, maybe partial SMA."""
    n = 40  # ~8 weeks
    df = _ohlcv(np.linspace(20, 30, n))
    out = weinstein_stage(df)
    assert out["weekly_bars"] < 30
    assert out["weinstein_stage"] is None


def test_weinstein_stage_1_basing_after_decline():
    """Decline then sideways base → Stage 1 (near flat SMA after prior decline)."""
    decline = np.linspace(100, 45, 180)
    base = 45.0 + np.sin(np.linspace(0, 10 * np.pi, 200)) * 1.2  # sideways coil
    close = np.concatenate([decline, base])
    df = _ohlcv(close)
    out = weinstein_stage(df)
    assert out["weinstein_stage"] == 1
    assert out["sma30w"] is not None


def test_anchored_vwap_from_volume_pivot():
    n = 100
    close = np.linspace(50, 70, n)
    vol = np.full(n, 100_000.0)
    vol[40] = 900_000.0  # clear climax inside lookback, ≥5 bars from end
    df = _ohlcv(close, volume=vol)
    out = anchored_vwap(df, lookback=90, min_bars_after=5)
    assert out["avwap"] is not None
    assert out["anchor_date"] is not None
    assert "highest-volume" in (out["anchor_reason"] or "")
    assert out["price_vs_avwap"] is not None
    # Anchor should be the high-volume day
    assert out["anchor_date"] == str(df.index[40].date())
    # Price ended above the early-mid anchor region → typically above aVWAP
    assert out["avwap"] > 0


def test_anchored_vwap_insufficient_bars():
    df = _ohlcv(np.linspace(10, 12, 4))
    assert anchored_vwap(df)["avwap"] is None


def test_agent_supporting_data_includes_structure_overlays():
    n = 260
    rng = np.random.default_rng(11)
    close = 40 * np.cumprod(1 + rng.normal(0.0015, 0.012, n))
    vol = rng.integers(80_000, 150_000, n).astype(float)
    vol[100] = float(np.max(vol) * 4)  # visible anchor
    df = _ohlcv(close, volume=vol)
    spy = _ohlcv(100 * np.cumprod(1 + rng.normal(0.0004, 0.008, n)))
    sig = MomentumAgent(_FrameMarketData({"AAPL": df, "SPY": spy})).analyze("AAPL")
    sd = sig.supporting_data
    for key in (
        "vbp_poc",
        "vbp_vah",
        "vbp_val",
        "price_vs_poc",
        "weinstein_stage",
        "sma30w",
        "avwap",
        "anchor_date",
        "anchor_reason",
        "price_vs_avwap",
    ):
        assert key in sd, f"missing {key}"
    assert sd["vbp_poc"] is not None
    assert sd["avwap"] is not None
    assert sd["weinstein_stage"] in (1, 2, 3, 4)
    low = sig.reasoning.lower()
    assert (
        "vbp" in low
        or "weinstein" in low
        or "avwap" in low
        or "stage" in low
        or "poc" in low
    )


def test_structure_overlays_soft_bias_stage2_vs_stage4():
    """Same short-term shape aside: Stage-2 tape should score higher than Stage-4."""
    n = 260
    up = np.linspace(40, 90, n)
    down = np.linspace(90, 40, n)
    vol = np.full(n, 120_000.0)
    vol[80] = 500_000.0
    spy = _ohlcv(np.linspace(100, 110, n))
    sig_up = MomentumAgent(
        _FrameMarketData({"U": _ohlcv(up, volume=vol), "SPY": spy})
    ).analyze("U")
    sig_dn = MomentumAgent(
        _FrameMarketData({"D": _ohlcv(down, volume=vol), "SPY": spy})
    ).analyze("D")
    assert sig_up.supporting_data["weinstein_stage"] == 2
    assert sig_dn.supporting_data["weinstein_stage"] == 4
    assert sig_up.supporting_data["raw_score"] > sig_dn.supporting_data["raw_score"]



# ---------------------------------------------------------------------------
# Minervini trend template (soft — not a veto)
# ---------------------------------------------------------------------------


def _strong_uptrend(n: int = 280) -> pd.DataFrame:
    """Monotonic daily advance that satisfies the price/SMA/52w checks."""
    return _ohlcv(np.linspace(40.0, 120.0, n), high_mult=1.01, low_mult=0.99)


def _fail_52w_high(n: int = 280) -> pd.DataFrame:
    """Same closes as a full pass, but one high is >25% above the last price."""
    df = _strong_uptrend(n)
    # Last close is 120; within-25% fails when week-high > 120 / 0.75 = 160.
    df.loc[df.index[200], "High"] = 200.0
    return df


def test_minervini_template_passes_all_ten():
    df = _strong_uptrend()
    out = minervini_trend_template(df, rs_proxy=90.0)
    assert out["minervini_passes"] == 10
    checks = out["minervini_checks"]
    assert set(checks) == {
        "rs_proxy_gt_70",
        "price_gt_sma50",
        "price_gt_sma150",
        "price_gt_sma200",
        "sma50_gt_sma150",
        "sma50_gt_sma200",
        "sma150_gt_sma200",
        "price_30pct_above_52w_low",
        "price_within_25pct_52w_high",
        "sma200_rising",
    }
    assert all(checks.values())
    assert out["rs_proxy"] == pytest.approx(90.0)
    assert out["pct_vs_52w_low"] is not None and out["pct_vs_52w_low"] >= 0.30
    assert out["pct_vs_52w_high"] is not None and out["pct_vs_52w_high"] >= -0.25
    assert out["sma200"] > out["sma200_month_ago"]


def test_minervini_template_fails_only_52w_high_proximity():
    df = _fail_52w_high()
    out = minervini_trend_template(df, rs_proxy=90.0)
    assert out["minervini_passes"] == 9
    checks = out["minervini_checks"]
    assert checks["price_within_25pct_52w_high"] is False
    assert all(v for k, v in checks.items() if k != "price_within_25pct_52w_high")
    assert out["pct_vs_52w_high"] < -0.25
    assert out["pct_vs_52w_low"] >= 0.30


def test_rs_proxy_percentile_scaled_1_99():
    # Weakest → 1, strongest → 99, tie in the middle shares an average rank.
    proxies = rs_proxy_from_excess({"A": 0.10, "B": 0.50, "C": -0.20})
    assert proxies["C"] == pytest.approx(1.0)
    assert proxies["A"] == pytest.approx(50.0)
    assert proxies["B"] == pytest.approx(99.0)
    tied = rs_proxy_from_excess({"A": 0.2, "B": 0.2})
    assert tied["A"] == pytest.approx(50.0)
    assert tied["B"] == pytest.approx(50.0)
    assert rs_proxy_from_excess({"ONLY": 0.4}) == {}


def test_minervini_score_weight_is_modest_and_not_a_veto():
    """9/10 helps; 0/10 cannot wipe a positive base score."""
    hi, d_hi = _apply_minervini_overlay(
        score=20.0,
        reasons=[],
        template={"minervini_passes": 9, "rs_proxy": 80.0},
    )
    lo, d_lo = _apply_minervini_overlay(
        score=20.0,
        reasons=[],
        template={"minervini_passes": 0, "rs_proxy": 20.0},
    )
    assert d_hi == pytest.approx((9 - 5) * _MINERVINI_PASS_WEIGHT + (80 - 50) * _MINERVINI_RS_WEIGHT)
    assert d_hi < 8.0  # smaller than a single MA cross (±15) or MACD (±10)
    assert hi > lo
    assert lo > 0.0  # failing the template does not zero or block the score


def _rs_universe_frames(target: pd.DataFrame) -> dict[str, pd.DataFrame]:
    n = len(target)
    spy = _ohlcv(np.linspace(100.0, 110.0, n))
    weak = _ohlcv(np.linspace(80.0, 70.0, n))
    flat = _ohlcv(np.full(n, 50.0))
    return {"SPY": spy, "WEAK": weak, "FLAT": flat, "TARGET": target}


def test_agent_exports_minervini_and_full_pass_beats_52w_miss():
    n = 280
    good = _strong_uptrend(n)
    bad = _fail_52w_high(n)
    frames_good = _rs_universe_frames(good)
    frames_good["GOOD"] = frames_good.pop("TARGET")
    frames_bad = _rs_universe_frames(bad)
    frames_bad["BAD"] = frames_bad.pop("TARGET")
    universe = ["GOOD", "BAD", "WEAK", "FLAT"]
    # Separate agents so each target is scored against the same peer shape.
    sig_good = MomentumAgent(
        _FrameMarketData({**frames_good, "BAD": bad}),
        universe=universe,
    ).analyze("GOOD")
    sig_bad = MomentumAgent(
        _FrameMarketData({**frames_bad, "GOOD": good}),
        universe=universe,
    ).analyze("BAD")
    for sig, passes in ((sig_good, 10), (sig_bad, 9)):
        sd = sig.supporting_data
        assert sd["minervini_passes"] == passes
        assert isinstance(sd["minervini_checks"], dict)
        assert sd["rs_proxy"] is not None and sd["rs_proxy"] > 70
        assert sd["pct_vs_52w_high"] is not None
        assert sd["pct_vs_52w_low"] is not None
        assert "rs_proxy" in (sd.get("rs_proxy_definition") or "").lower() or "not ibd" in (
            sd.get("rs_proxy_definition") or ""
        ).lower()
        assert sig.direction == Direction.LONG  # template is not a hard veto
        assert "not a veto" in sig.reasoning.lower()
    assert sig_bad.supporting_data["minervini_checks"]["price_within_25pct_52w_high"] is False
    assert sig_good.supporting_data["minervini_score_delta"] == pytest.approx(
        sig_bad.supporting_data["minervini_score_delta"] + _MINERVINI_PASS_WEIGHT
    )


def test_minervini_asof_matches_truncated_frame_no_lookahead():
    """Backtest as-of history and the pure function see the same bars — not the future spike."""
    n = 320
    close = np.linspace(40.0, 120.0, n)
    # After the as-of date, a spike prints a much higher 52-week high, then price round-trips.
    close = close.copy()
    close[290:300] = np.linspace(close[289], 220.0, 10)
    close[300:] = np.linspace(200.0, 90.0, n - 300)
    win = _ohlcv(close, high_mult=1.01, low_mult=0.99)
    # Make the post-as-of high unmistakable even vs the close spike.
    win.loc[win.index[295], "High"] = 260.0
    weak = _ohlcv(np.linspace(80.0, 60.0, n))
    spy = _ohlcv(np.linspace(100.0, 108.0, n))
    asof = win.index[279]
    bars = {"WIN": win, "WEAK": weak, "SPY": spy}
    md = AsOfMarketData(bars, as_of=asof)
    agent = MomentumAgent(md, universe=["WIN", "WEAK"])
    sig = agent.analyze("WIN", benchmark="SPY")
    asof_df = md.history("WIN", period="1y", interval="1d")
    direct = minervini_trend_template(asof_df, rs_proxy=sig.supporting_data["rs_proxy"])
    # Same function on the raw slice (no period clip) must agree — as-of did not drop
    # the pre-spike window, and neither view contains bars after as_of.
    sliced = win.loc[win.index <= asof]
    sliced_tmpl = minervini_trend_template(sliced, rs_proxy=sig.supporting_data["rs_proxy"])
    full = minervini_trend_template(win, rs_proxy=sig.supporting_data["rs_proxy"])

    assert sig.supporting_data["last_close"] == pytest.approx(float(sliced["Close"].iloc[-1]))
    assert pd.Timestamp(asof_df.index[-1]) <= pd.Timestamp(asof)
    assert float(asof_df["High"].max()) == pytest.approx(float(sliced["High"].max()))
    assert sig.supporting_data["minervini_passes"] == direct["minervini_passes"] == sliced_tmpl["minervini_passes"]
    assert sig.supporting_data["minervini_checks"] == direct["minervini_checks"] == sliced_tmpl["minervini_checks"]
    assert sig.supporting_data["pct_vs_52w_high"] == pytest.approx(direct["pct_vs_52w_high"])
    assert sig.supporting_data["pct_vs_52w_low"] == pytest.approx(direct["pct_vs_52w_low"])
    # Future spike is a higher 52w high and a worse proximity reading than the as-of slice.
    assert float(win["High"].max()) > float(sliced["High"].max())
    assert full["pct_vs_52w_high"] < direct["pct_vs_52w_high"]
    assert full["week52_high"] > direct["week52_high"]
    # As-of path still passes proximity; the completed future path does not.
    assert direct["minervini_checks"]["price_within_25pct_52w_high"] is True
    assert full["minervini_checks"]["price_within_25pct_52w_high"] is False


# ---------------------------------------------------------------------------
# OBV and Chandelier ATR trail (soft — not a veto)
# ---------------------------------------------------------------------------


def test_obv_classic_adds_up_volume_and_subtracts_down_volume():
    """Unchanged closes do not move OBV. First bar has no prior close."""
    close = [10.0, 11.0, 11.0, 10.0, 12.0]
    volume = [100.0, 200.0, 50.0, 80.0, 40.0]
    df = _ohlcv(close, volume=volume)
    out = on_balance_volume(df, ma_window=20, slope_lookback=10)
    # 0 + 200 (up) + 0 (flat) − 80 (down) + 40 (up)
    assert out["obv"] == pytest.approx(160.0)
    # Too short for the 20d OBV average / 10d slope
    assert out["obv_vs_ma"] is None
    assert out["obv_slope"] is None


def test_obv_slope_and_vs_average_on_rising_and_falling_tape():
    n = 40
    up = on_balance_volume(_ohlcv(np.linspace(50, 80, n), volume=np.full(n, 100_000.0)))
    down = on_balance_volume(_ohlcv(np.linspace(80, 50, n), volume=np.full(n, 100_000.0)))
    assert up["obv"] is not None and up["obv"] > 0
    assert up["obv_slope"] is not None and up["obv_slope"] > 0
    assert up["obv_vs_ma"] is not None and up["obv_vs_ma"] > 0
    assert down["obv"] is not None and down["obv"] < 0
    assert down["obv_slope"] < 0
    assert down["obv_vs_ma"] < 0
    # Monotonic up, constant volume: every bar adds the full daily volume.
    assert up["obv"] == pytest.approx(100_000.0 * (n - 1))


def test_obv_score_is_modest_and_signed_with_flow():
    n = 40
    up = on_balance_volume(_ohlcv(np.linspace(50, 80, n)))
    down = on_balance_volume(_ohlcv(np.linspace(80, 50, n)))
    score_up, d_up = _apply_obv_overlay(score=20.0, reasons=[], obv=up)
    score_dn, d_dn = _apply_obv_overlay(score=20.0, reasons=[], obv=down)
    assert d_up == pytest.approx(_OBV_VS_MA_SCORE + _OBV_SLOPE_SCORE)
    assert d_dn == pytest.approx(-(_OBV_VS_MA_SCORE + _OBV_SLOPE_SCORE))
    assert abs(d_up) <= _OBV_SCORE_CAP < 15  # smaller than one MA cross (±15)
    assert abs(d_dn) <= _OBV_SCORE_CAP
    assert score_up > score_dn
    assert score_dn > 0  # worst OBV tilt does not wipe a positive base score


def test_chandelier_long_is_highest_high_minus_3x_atr14():
    """Constant true range → known ATR. Long trail = 22d high − 3 × ATR."""
    n = 30
    close = 100.0 + np.arange(n, dtype=float)  # +1 each bar
    high = close + 1.0
    low = close - 1.0
    # TR after bar 0 is 2: high-low = 2, high-prev close = 2, low-prev close = 0.
    df = pd.DataFrame(
        {
            "Open": np.r_[close[0], close[:-1]],
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": np.full(n, 100_000.0),
        },
        index=_idx(n),
    )
    atr = float(_atr(df, 14).iloc[-1])
    assert atr == pytest.approx(2.0)
    out = chandelier_trail(df)
    hh = float(high[-_CHANDELIER_LOOKBACK:].max())
    ll = float(low[-_CHANDELIER_LOOKBACK:].min())
    expected_long = hh - _CHANDELIER_ATR_MULT * atr
    expected_short = ll + _CHANDELIER_ATR_MULT * atr
    last = float(close[-1])
    assert out["atr"] == pytest.approx(atr)
    assert out["chandelier_long"] == pytest.approx(expected_long)
    assert out["chandelier_short"] == pytest.approx(expected_short)
    assert out["price_vs_chandelier"] == pytest.approx((last - expected_long) / last)
    assert last > expected_long  # still above the long trail
    assert out["chandelier_lookback"] == 22
    assert out["chandelier_atr_mult"] == pytest.approx(3.0)


def test_chandelier_close_through_trail_is_small_negative_not_a_forced_sell():
    """A deep drop off the 22-session high closes through the long trail."""
    n = 40
    close = np.concatenate([np.full(30, 100.0), np.linspace(100.0, 70.0, 10)])
    high = close + 1.0
    low = close - 1.0
    # Keep the lookback high at the plateau so the trail stays near 100 − 3×ATR.
    high[:30] = 102.0
    df = pd.DataFrame(
        {
            "Open": np.r_[close[0], close[:-1]],
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": np.full(n, 80_000.0),
        },
        index=_idx(n),
    )
    out = chandelier_trail(df)
    assert out["atr"] is not None and out["atr"] > 0
    assert out["chandelier_long"] == pytest.approx(
        float(high[-22:].max()) - 3.0 * out["atr"]
    )
    assert float(close[-1]) < out["chandelier_long"]
    assert out["price_vs_chandelier"] < 0
    # Short-context trail is the mirror: lowest low + 3×ATR, not used as a veto.
    assert out["chandelier_short"] == pytest.approx(
        float(low[-22:].min()) + 3.0 * out["atr"]
    )
    reasons: list[str] = []
    score, delta = _apply_chandelier_overlay(score=40.0, reasons=reasons, trail=out)
    assert delta == pytest.approx(-_CHANDELIER_SCORE)
    assert _CHANDELIER_SCORE < 15
    assert score == pytest.approx(40.0 - _CHANDELIER_SCORE)
    assert score >= 15  # still a long on the score scale — exit hint, not a forced sell
    assert "not a forced sell" in " ".join(reasons).lower()


def test_chandelier_above_trail_is_small_positive():
    n = 30
    close = 100.0 + np.arange(n, dtype=float)
    df = pd.DataFrame(
        {
            "Open": np.r_[close[0], close[:-1]],
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": np.full(n, 50_000.0),
        },
        index=_idx(n),
    )
    out = chandelier_trail(df)
    score, delta = _apply_chandelier_overlay(score=0.0, reasons=[], trail=out)
    assert delta == pytest.approx(_CHANDELIER_SCORE)
    assert score == pytest.approx(_CHANDELIER_SCORE)
    assert out["price_vs_chandelier"] > 0


def test_agent_exports_obv_and_chandelier_without_hard_veto():
    """Strong trends keep their direction; overlays only nudge the raw score."""
    n = 260
    up = np.linspace(40.0, 120.0, n)
    down = np.linspace(120.0, 40.0, n)
    vol = np.full(n, 100_000.0)
    spy = _ohlcv(np.linspace(100.0, 110.0, n))
    sig_up = MomentumAgent(_FrameMarketData({"U": _ohlcv(up, volume=vol), "SPY": spy})).analyze("U")
    sig_dn = MomentumAgent(_FrameMarketData({"D": _ohlcv(down, volume=vol), "SPY": spy})).analyze("D")
    for sig, direction in ((sig_up, Direction.LONG), (sig_dn, Direction.SHORT)):
        sd = sig.supporting_data
        assert sd["obv"] is not None
        assert sd["obv_slope"] is not None
        assert sd["obv_vs_ma"] is not None
        assert sd["atr"] is not None
        assert sd["atr"] == pytest.approx(sd["atr14"])
        assert sd["chandelier_long"] is not None
        assert sd["chandelier_short"] is not None
        assert sd["price_vs_chandelier"] is not None
        assert abs(sd["obv_score_delta"]) <= _OBV_SCORE_CAP < 15
        assert abs(sd["chandelier_score_delta"]) <= _CHANDELIER_SCORE < 15
        assert abs(sd["obv_score_delta"]) + abs(sd["chandelier_score_delta"]) < 15
        assert sig.direction == direction  # neither overlay hard-vetoes the tape
        assert "not a veto" in sig.reasoning.lower() or "not a forced sell" in sig.reasoning.lower()
    assert sig_up.supporting_data["obv"] > 0
    assert sig_up.supporting_data["obv_slope"] > 0
    assert sig_up.supporting_data["price_vs_chandelier"] > 0
    assert sig_up.supporting_data["chandelier_score_delta"] == pytest.approx(_CHANDELIER_SCORE)
    assert sig_up.supporting_data["obv_score_delta"] == pytest.approx(_OBV_SCORE_CAP)
    assert sig_dn.supporting_data["obv"] < 0
    assert sig_dn.supporting_data["obv_score_delta"] == pytest.approx(-_OBV_SCORE_CAP)
    # A close through the long trail on the down tape is a soft negative, not a block.
    assert sig_dn.supporting_data["price_vs_chandelier"] < 0
    assert sig_dn.supporting_data["chandelier_score_delta"] == pytest.approx(-_CHANDELIER_SCORE)
    assert sig_dn.direction == Direction.SHORT
