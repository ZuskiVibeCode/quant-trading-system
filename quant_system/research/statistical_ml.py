"""StatisticalMLAgent — mean-reversion / momentum-stat features (numpy/pandas only)."""

from __future__ import annotations

import math
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd

from quant_system.market_data.interface import MarketData
from quant_system.models.signal import Direction, Signal
from quant_system.research.base import ResearchAgent

# Default peer set for cheap cross-sectional rank (theme universe).
_DEFAULT_UNIVERSE: tuple[str, ...] = (
    "IONQ",
    "RGTI",
    "QUBT",
    "IBM",
    "GOOGL",
    "QBTS",
    "QSI",
    "MSFT",
    "AMD",
    "HON",
    "RKLB",
    "ASTS",
    "LUNR",
    "BA",
    "LHX",
    "RDW",
    "BKSY",
    "IRDM",
    "NOC",
    "LMT",
    "ISRG",
    "PATH",
    "TER",
    "NVDA",
    "TSLA",
    "CGNX",
    "SYM",
    "ROK",
    "AVAV",
    "ABB",
)

HistoryFetcher = Callable[[str], pd.DataFrame]
PeerMomFetcher = Callable[[str], float | None]


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or math.isinf(f):  # NaN / inf
        return None
    return f


def _close_series(df: pd.DataFrame | None) -> pd.Series | None:
    if df is None or getattr(df, "empty", True):
        return None
    if "Close" not in df.columns:
        return None
    close = pd.to_numeric(df["Close"], errors="coerce").dropna()
    if close.empty:
        return None
    return close.astype(float)


def compute_stat_features(close: pd.Series) -> dict[str, Any]:
    """Compute mean-reversion / momentum / vol-regime features from a close series.

    Returns a dict of floats / strings; empty close → empty features.
    """
    if close is None or len(close) == 0:
        return {}

    close = pd.to_numeric(close, errors="coerce").dropna().astype(float)
    n = len(close)
    if n < 5:
        return {"bars": n}

    rets = close.pct_change()
    last = float(close.iloc[-1])

    ma_windows = (10, 20, 50)
    mas: dict[int, float | None] = {}
    residuals: dict[int, float | None] = {}
    z_to_ma: dict[int, float | None] = {}

    for w in ma_windows:
        if n < max(5, w // 2):
            mas[w] = None
            residuals[w] = None
            z_to_ma[w] = None
            continue
        ma = close.rolling(w, min_periods=max(5, w // 2)).mean()
        ma_v = _safe_float(ma.iloc[-1])
        mas[w] = ma_v
        if ma_v is None or ma_v == 0:
            residuals[w] = None
            z_to_ma[w] = None
            continue
        resid_pct = (last - ma_v) / ma_v
        residuals[w] = resid_pct
        # Z vs rolling residual distribution (price - MA) / rolling std of close
        roll_std = close.rolling(w, min_periods=max(5, w // 2)).std(ddof=1)
        sigma = _safe_float(roll_std.iloc[-1]) or 1e-9
        z_to_ma[w] = (last - ma_v) / sigma

    # Return z-score (last return vs lookback window)
    lookback = min(60, max(10, n - 1))
    window = rets.iloc[-lookback:].dropna()
    mu = _safe_float(window.mean()) if len(window) else None
    sigma_ret = _safe_float(window.std(ddof=1)) if len(window) >= 2 else None
    last_ret = _safe_float(rets.iloc[-1])
    if last_ret is not None and mu is not None and sigma_ret and sigma_ret > 0:
        z_last_ret = (last_ret - mu) / sigma_ret
    else:
        z_last_ret = None

    def _mom(days: int) -> float | None:
        if n <= days:
            return None
        past = _safe_float(close.iloc[-(days + 1)])
        if past is None or past == 0:
            return None
        return last / past - 1.0

    mom5 = _mom(5)
    mom20 = _mom(20)
    mom60 = _mom(60)

    # Realized vol regime: short vs long realized vol (annualized-ish ratio)
    vol_short = None
    vol_long = None
    vol_regime = "unknown"
    vol_ratio = None
    if len(rets.dropna()) >= 20:
        vol_short = _safe_float(rets.iloc[-20:].std(ddof=1))
    if len(rets.dropna()) >= 60:
        vol_long = _safe_float(rets.iloc[-60:].std(ddof=1))
    elif len(rets.dropna()) >= 30:
        vol_long = _safe_float(rets.iloc[-30:].std(ddof=1))

    if vol_short is not None and vol_long is not None and vol_long > 1e-12:
        vol_ratio = vol_short / vol_long
        if vol_ratio >= 1.35:
            vol_regime = "high"
        elif vol_ratio <= 0.75:
            vol_regime = "low"
        else:
            vol_regime = "normal"
    elif vol_short is not None:
        vol_regime = "normal"

    return {
        "bars": n,
        "last_close": last,
        "ma10": mas.get(10),
        "ma20": mas.get(20),
        "ma50": mas.get(50),
        "residual_to_ma10": residuals.get(10),
        "residual_to_ma20": residuals.get(20),
        "residual_to_ma50": residuals.get(50),
        "z_to_ma10": z_to_ma.get(10),
        "z_to_ma20": z_to_ma.get(20),
        "z_to_ma50": z_to_ma.get(50),
        "z_last_ret": z_last_ret,
        "mu_ret": mu,
        "sigma_ret": sigma_ret,
        "last_ret": last_ret,
        "mom5": mom5,
        "mom20": mom20,
        "mom60": mom60,
        "vol20": vol_short,
        "vol60": vol_long,
        "vol_ratio": vol_ratio,
        "vol_regime": vol_regime,
    }


def cross_sectional_mom_rank(
    ticker: str,
    mom20: float | None,
    peer_moms: dict[str, float],
) -> dict[str, Any]:
    """Percentile rank of ticker mom20 among peers (inclusive of self if present)."""
    if mom20 is None or not peer_moms:
        return {
            "xs_mom20_rank": None,
            "xs_peer_count": 0,
            "xs_universe_size": len(peer_moms),
        }
    values = list(peer_moms.values())
    # Ensure self is represented
    key = ticker.upper()
    if key not in peer_moms:
        values.append(mom20)
    arr = np.asarray(values, dtype=float)
    # Percentile: fraction of peers with mom <= ours
    rank = float(np.mean(arr <= mom20))
    return {
        "xs_mom20_rank": rank,
        "xs_peer_count": int(len(arr)),
        "xs_universe_size": int(len(peer_moms)),
    }


def score_stat_features(
    features: dict[str, Any],
    *,
    xs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map features → direction, confidence, statistical_edge_score, uncertainty."""
    if not features or features.get("bars", 0) < 30:
        return {
            "direction": Direction.NEUTRAL,
            "confidence": 8.0,
            "statistical_edge_score": 0.0,
            "predicted_direction": "neutral",
            "uncertainty_note": "insufficient history for stable statistical features",
            "confidence_interval": {"low": 0.0, "high": 15.0, "unit": "confidence_points"},
            "reasoning": "Insufficient history for statistical features",
            "raw_score": 0.0,
            "components": {},
        }

    reasons: list[str] = []
    components: dict[str, float] = {}
    score = 0.0

    z20 = _safe_float(features.get("z_to_ma20"))
    resid20 = _safe_float(features.get("residual_to_ma20"))
    z_ret = _safe_float(features.get("z_last_ret"))
    mom20 = _safe_float(features.get("mom20"))
    mom60 = _safe_float(features.get("mom60"))
    mom5 = _safe_float(features.get("mom5"))
    vol_regime = str(features.get("vol_regime") or "unknown")
    vol_ratio = _safe_float(features.get("vol_ratio"))

    # --- Mean reversion (dominant when |z| extreme) ---
    mr = 0.0
    if z20 is not None:
        if z20 <= -2.0:
            mr += 22
            reasons.append(f"deeply oversold vs MA20 (z={z20:.2f})")
        elif z20 <= -1.25:
            mr += 14
            reasons.append(f"oversold vs MA20 (z={z20:.2f})")
        elif z20 >= 2.0:
            mr -= 22
            reasons.append(f"deeply overbought vs MA20 (z={z20:.2f})")
        elif z20 >= 1.25:
            mr -= 14
            reasons.append(f"overbought vs MA20 (z={z20:.2f})")
        else:
            reasons.append(f"price near MA20 (z={z20:.2f})")
        if resid20 is not None and abs(z20) >= 1.25:
            reasons.append(f"residual_to_MA20={resid20:+.1%}")
    components["mean_reversion"] = mr
    score += mr

    # Mild return z contribution (mean-revert last shock)
    if z_ret is not None:
        if z_ret <= -1.5:
            score += 6
            components["ret_z"] = 6.0
            reasons.append(f"return z oversold ({z_ret:.2f})")
        elif z_ret >= 1.5:
            score -= 6
            components["ret_z"] = -6.0
            reasons.append(f"return z overbought ({z_ret:.2f})")
        else:
            components["ret_z"] = 0.0

    # --- Momentum-stat ---
    mom_score = 0.0
    extreme_mr = z20 is not None and abs(z20) >= 1.75
    if mom20 is not None:
        if mom20 > 0.08:
            mom_score += 16
            reasons.append(f"strong 20d momentum {mom20:+.1%}")
        elif mom20 > 0.03:
            mom_score += 10
            reasons.append(f"positive 20d momentum {mom20:+.1%}")
        elif mom20 < -0.08:
            mom_score -= 16
            reasons.append(f"strong 20d down-momentum {mom20:+.1%}")
        elif mom20 < -0.03:
            mom_score -= 10
            reasons.append(f"negative 20d momentum {mom20:+.1%}")
        else:
            reasons.append(f"flat 20d momentum {mom20:+.1%}")

    if mom60 is not None:
        if mom60 > 0.12:
            mom_score += 10
            reasons.append(f"60d trend up {mom60:+.1%}")
        elif mom60 < -0.12:
            mom_score -= 10
            reasons.append(f"60d trend down {mom60:+.1%}")

    if mom5 is not None and abs(mom5) >= 0.04 and not extreme_mr:
        mom_score += 4.0 if mom5 > 0 else -4.0

    # When deeply extended, dampen momentum so mean-reversion can win
    if extreme_mr:
        mom_score *= 0.25
        reasons.append("momentum dampened (extreme MA residual)")

    components["momentum"] = mom_score
    score += mom_score

    # --- Cross-sectional rank ---
    xs = xs or {}
    xs_rank = _safe_float(xs.get("xs_mom20_rank"))
    xs_comp = 0.0
    if xs_rank is not None:
        # High rank → relative strength long; low → short
        if xs_rank >= 0.8:
            xs_comp = 8.0
            reasons.append(f"cross-sectional mom rank top ({xs_rank:.0%})")
        elif xs_rank <= 0.2:
            xs_comp = -8.0
            reasons.append(f"cross-sectional mom rank bottom ({xs_rank:.0%})")
        else:
            reasons.append(f"cross-sectional mom rank mid ({xs_rank:.0%})")
        if extreme_mr:
            xs_comp *= 0.2
            reasons.append("cross-sectional dampened (extreme MA residual)")
    components["cross_sectional"] = xs_comp
    score += xs_comp

    # --- Vol regime: scale trend/xs legs only (not mean-reversion) ---
    vol_scale = 1.0
    if vol_regime == "high":
        vol_scale = 0.75
        reasons.append(f"high vol regime (ratio={vol_ratio:.2f})" if vol_ratio else "high vol regime")
    elif vol_regime == "low":
        vol_scale = 1.1
        reasons.append("low vol regime")
    else:
        reasons.append(f"vol regime={vol_regime}")
    components["vol_scale"] = vol_scale
    # Recompute: MR stays full strength; mom+xs scaled by vol
    score = components.get("mean_reversion", 0.0) + components.get("ret_z", 0.0)
    score += (components.get("momentum", 0.0) + components.get("cross_sectional", 0.0)) * vol_scale

    # Direction thresholds
    if score >= 12:
        direction = Direction.LONG
        predicted = "long"
    elif score <= -12:
        direction = Direction.SHORT
        predicted = "short"
    else:
        direction = Direction.NEUTRAL
        predicted = "neutral"

    edge = float(np.clip(score, -100, 100))
    # Confidence from |edge|; high-vol pulls toward uncertainty band
    base_conf = 42.0 + abs(edge) * 1.35
    if vol_regime == "high":
        base_conf -= 8.0
    if features.get("bars", 0) < 60:
        base_conf -= 6.0
    confidence = float(np.clip(base_conf, 5.0, 88.0))
    if math.isnan(confidence):
        confidence = 10.0

    half_width = max(6.0, 18.0 - abs(edge) * 0.15)
    if vol_regime == "high":
        half_width += 5.0
    ci_low = float(np.clip(confidence - half_width, 0, 100))
    ci_high = float(np.clip(confidence + half_width, 0, 100))

    uncertainty = (
        f"confidence band ~[{ci_low:.0f}, {ci_high:.0f}]; "
        f"vol_regime={vol_regime}; bars={features.get('bars')}"
    )
    if xs_rank is None:
        uncertainty += "; no cross-sectional rank"

    return {
        "direction": direction,
        "confidence": confidence,
        "statistical_edge_score": edge,
        "predicted_direction": predicted,
        "uncertainty_note": uncertainty,
        "confidence_interval": {
            "low": ci_low,
            "high": ci_high,
            "unit": "confidence_points",
        },
        "reasoning": "; ".join(reasons) if reasons else "No clear statistical edge",
        "raw_score": edge,
        "components": components,
    }


def _ohlcv_from_close(close: pd.Series) -> pd.DataFrame:
    """Build minimal OHLCV frame from a close series (for tests / injectors)."""
    close = pd.to_numeric(close, errors="coerce").astype(float)
    if not isinstance(close.index, pd.DatetimeIndex):
        idx = pd.date_range(end=pd.Timestamp.utcnow().normalize(), periods=len(close), freq="B")
        close = pd.Series(close.to_numpy(), index=idx[: len(close)])
    high = close * 1.005
    low = close * 0.995
    open_ = close.shift(1).fillna(close.iloc[0])
    vol = pd.Series(1_000_000, index=close.index)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=close.index,
    )


class StatisticalMLAgent(ResearchAgent):
    """Mean-reversion + momentum-stat blend with optional cross-sectional rank.

    v1 uses numpy/pandas only (no sklearn). OOS supervised models remain a future
    enhancement on a frozen feature store — this agent never fits weights in-sample.
    """

    agent_id = "statistical_ml"

    def __init__(
        self,
        market_data: MarketData,
        *,
        history_fetcher: HistoryFetcher | None = None,
        peer_mom_fetcher: PeerMomFetcher | None = None,
        universe: Iterable[str] | None = None,
        enable_cross_sectional: bool = True,
    ):
        super().__init__(market_data)
        self._history_fetcher = history_fetcher
        self._peer_mom_fetcher = peer_mom_fetcher
        self._universe = tuple(t.upper() for t in (universe or _DEFAULT_UNIVERSE))
        self._enable_xs = enable_cross_sectional

    def _load_history(self, ticker: str) -> pd.DataFrame | None:
        try:
            if self._history_fetcher is not None:
                df = self._history_fetcher(ticker)
            else:
                df = self.market_data.history(ticker, period="1y", interval="1d")
        except Exception:  # noqa: BLE001 — missing data path
            return None
        if df is None or getattr(df, "empty", True):
            return None
        return df

    def _peer_moms(self, ticker: str) -> dict[str, float]:
        """Cheap peer 20d momentum map; skips failures silently."""
        if not self._enable_xs:
            return {}
        out: dict[str, float] = {}
        for peer in self._universe:
            try:
                if self._peer_mom_fetcher is not None:
                    m = self._peer_mom_fetcher(peer)
                    if m is not None:
                        out[peer] = float(m)
                    continue
                df = self._load_history(peer)
                close = _close_series(df)
                if close is None or len(close) < 22:
                    continue
                m20 = float(close.iloc[-1] / close.iloc[-21] - 1)
                out[peer] = m20
            except Exception:  # noqa: BLE001
                continue
            # Cap work: if we already have a decent peer set, stop early when
            # scanning live (history_fetcher path already cheap/deterministic).
            if self._history_fetcher is None and self._peer_mom_fetcher is None and len(out) >= 8:
                # Still include self if missing and we have capacity
                if ticker.upper() not in out and len(out) < 12:
                    continue
                if len(out) >= 10:
                    break
        return out

    def analyze(self, ticker: str, *, benchmark: str = "SPY") -> Signal:
        ticker_u = ticker.strip().upper()
        df = self._load_history(ticker_u)
        close = _close_series(df)

        if close is None or len(close) < 30:
            return Signal(
                agent_id=self.agent_id,
                ticker=ticker_u,
                direction=Direction.NEUTRAL,
                confidence=8.0,
                reasoning="Insufficient history for statistical features",
                supporting_data={
                    "stub": False,
                    "bars": 0 if close is None else int(len(close)),
                    "statistical_edge_score": 0.0,
                    "predicted_direction": "neutral",
                    "uncertainty_note": "missing or insufficient price history",
                    "confidence_interval": {
                        "low": 0.0,
                        "high": 15.0,
                        "unit": "confidence_points",
                    },
                    "benchmark": benchmark,
                },
            )

        features = compute_stat_features(close)
        peer_moms = self._peer_moms(ticker_u)
        # Ensure self mom is in peer map when we have it
        if features.get("mom20") is not None and ticker_u not in peer_moms:
            peer_moms = {**peer_moms, ticker_u: float(features["mom20"])}
        xs = cross_sectional_mom_rank(ticker_u, features.get("mom20"), peer_moms)
        scored = score_stat_features(features, xs=xs)

        supporting: dict[str, Any] = {
            "stub": False,
            "benchmark": benchmark,
            "statistical_edge_score": scored["statistical_edge_score"],
            "predicted_direction": scored["predicted_direction"],
            "uncertainty_note": scored["uncertainty_note"],
            "confidence_interval": scored["confidence_interval"],
            "components": scored["components"],
            "raw_score": scored["raw_score"],
            **{k: features.get(k) for k in (
                "bars",
                "last_close",
                "ma10",
                "ma20",
                "ma50",
                "residual_to_ma10",
                "residual_to_ma20",
                "residual_to_ma50",
                "z_to_ma10",
                "z_to_ma20",
                "z_to_ma50",
                "z_last_ret",
                "mom5",
                "mom20",
                "mom60",
                "vol20",
                "vol60",
                "vol_ratio",
                "vol_regime",
            )},
            **xs,
        }

        return Signal(
            agent_id=self.agent_id,
            ticker=ticker_u,
            direction=scored["direction"],
            confidence=float(scored["confidence"]),
            reasoning=scored["reasoning"],
            supporting_data=supporting,
        )


__all__ = [
    "StatisticalMLAgent",
    "compute_stat_features",
    "score_stat_features",
    "cross_sectional_mom_rank",
    "_ohlcv_from_close",
]
