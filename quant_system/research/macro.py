"""MacroAgent — risk-on / risk-off / neutral regime from free market proxies."""

from __future__ import annotations

import math
from typing import Any, Callable

import pandas as pd

from quant_system.market_data.interface import MarketData
from quant_system.models.signal import Direction, Signal
from quant_system.research.base import ResearchAgent

# Theme membership for sector-bias overlays (high-beta growth themes).
_THEME_TICKERS: dict[str, str] = {
    t: theme
    for theme, tickers in {
        "quantum": ("IONQ", "RGTI", "QUBT", "IBM", "GOOGL"),
        "space": ("RKLB", "ASTS", "LUNR", "BA", "LHX"),
        "robotics": ("ISRG", "PATH", "TER", "NVDA", "TSLA"),
    }.items()
    for t in tickers
}

# Ordered fallbacks for each proxy family (first successful history wins).
_VIX_TICKERS = ("^VIX", "VIX")
_DOLLAR_TICKERS = ("UUP", "DX-Y.NYB", "DX=F")
_RATES_TICKERS = ("TLT", "^TNX", "IEF")

ProxiesFetcher = Callable[[str], dict[str, Any]]


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


def _last_close(df: pd.DataFrame) -> float | None:
    if df is None or df.empty or "Close" not in df.columns:
        return None
    return _safe_float(df["Close"].iloc[-1])


def _sma_last(close: pd.Series, window: int, *, min_periods: int | None = None) -> float | None:
    mp = min_periods if min_periods is not None else max(1, window // 2)
    if len(close) < mp:
        return None
    v = close.rolling(window, min_periods=mp).mean().iloc[-1]
    return _safe_float(v)


def _pct_change_n(close: pd.Series, n: int) -> float | None:
    if len(close) <= n:
        return None
    a = _safe_float(close.iloc[-1])
    b = _safe_float(close.iloc[-(n + 1)])
    if a is None or b is None or b == 0:
        return None
    return (a / b) - 1.0


def _try_history(market_data: MarketData, tickers: tuple[str, ...], period: str = "1y") -> tuple[str | None, pd.DataFrame | None, str | None]:
    """Return (used_ticker, dataframe, error). Never raises."""
    errors: list[str] = []
    for t in tickers:
        try:
            df = market_data.history(t, period=period, interval="1d")
            if df is not None and not df.empty and "Close" in df.columns:
                return t, df, None
            errors.append(f"{t}: empty")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{t}: {exc}")
    return None, None, "; ".join(errors) if errors else "no tickers"


def fetch_macro_proxies(market_data: MarketData, benchmark: str = "SPY") -> dict[str, Any]:
    """Pull VIX, dollar, rates, and equity-benchmark trend via MarketData.

    Returns a flat dict of levels / returns used by classify_regime.
    Missing series are omitted (caller treats as incomplete -> neutral).
    """
    out: dict[str, Any] = {"benchmark": benchmark.upper(), "errors": {}}

    # --- VIX ---
    vix_t, vix_df, vix_err = _try_history(market_data, _VIX_TICKERS, period="6mo")
    if vix_df is not None:
        out["vix_ticker"] = vix_t
        out["vix"] = _last_close(vix_df)
        out["vix_ma20"] = _sma_last(vix_df["Close"], 20, min_periods=10)
        out["vix_change_5d"] = _pct_change_n(vix_df["Close"], 5)
    elif vix_err:
        out["errors"]["vix"] = vix_err

    # --- Dollar ---
    dxy_t, dxy_df, dxy_err = _try_history(market_data, _DOLLAR_TICKERS, period="1y")
    if dxy_df is not None:
        out["dollar_ticker"] = dxy_t
        out["dollar_last"] = _last_close(dxy_df)
        out["dollar_ma50"] = _sma_last(dxy_df["Close"], 50, min_periods=20)
        out["dollar_change_20d"] = _pct_change_n(dxy_df["Close"], 20)
    elif dxy_err:
        out["errors"]["dollar"] = dxy_err

    # --- Rates (TLT preferred: rising TLT = falling yields = usually risk-supportive) ---
    rates_t, rates_df, rates_err = _try_history(market_data, _RATES_TICKERS, period="1y")
    if rates_df is not None:
        out["rates_ticker"] = rates_t
        out["rates_last"] = _last_close(rates_df)
        out["rates_ma50"] = _sma_last(rates_df["Close"], 50, min_periods=20)
        out["rates_change_20d"] = _pct_change_n(rates_df["Close"], 20)
        # If using ^TNX (yield index), invert interpretation flag
        out["rates_is_yield"] = bool(rates_t and ("TNX" in rates_t.upper() or rates_t.upper().startswith("^TN")))
    elif rates_err:
        out["errors"]["rates"] = rates_err

    # --- Equity benchmark trend ---
    try:
        bdf = market_data.history(benchmark, period="1y", interval="1d")
    except Exception as exc:  # noqa: BLE001
        bdf = None
        out["errors"]["benchmark"] = str(exc)

    if bdf is not None and not bdf.empty and "Close" in bdf.columns:
        close = bdf["Close"]
        out["spy_last"] = _last_close(bdf)
        out["spy_ma50"] = _sma_last(close, 50, min_periods=20)
        out["spy_ma200"] = _sma_last(close, 200, min_periods=50)
        out["spy_change_20d"] = _pct_change_n(close, 20)
        rets = close.pct_change().dropna()
        if len(rets) >= 20:
            out["realized_vol_20d_ann"] = _safe_float(rets.iloc[-20:].std(ddof=1) * (252**0.5))
    elif "benchmark" not in out["errors"]:
        out["errors"]["benchmark"] = "empty history"

    return out


def classify_regime(proxies: dict[str, Any]) -> dict[str, Any]:
    """Score proxies into risk_on / risk_off / neutral + macro_risk_level.

    Each available pillar votes +1 (risk-on), -1 (risk-off), or 0 (mixed).
    Sparse / missing data -> neutral + low confidence (never crashes).
    """
    if not proxies:
        return {
            "regime": "neutral",
            "direction": Direction.NEUTRAL,
            "confidence": 15.0,
            "macro_risk_level": "unknown",
            "raw_score": 0.0,
            "votes": {},
            "reasons": ["No macro proxy data available; emitting neutral with low confidence."],
            "key_levels": {},
            "coverage": 0,
        }

    score = 0.0
    votes: dict[str, float] = {}
    reasons: list[str] = []
    key_levels: dict[str, Any] = {}
    coverage = 0

    # --- VIX pillar ---
    vix = _safe_float(proxies.get("vix"))
    if vix is not None:
        coverage += 1
        key_levels["vix"] = vix
        key_levels["vix_ticker"] = proxies.get("vix_ticker")
        vix_ma = _safe_float(proxies.get("vix_ma20"))
        if vix_ma is not None:
            key_levels["vix_ma20"] = vix_ma
        if vix < 15:
            votes["vix"] = 1.5
            reasons.append(f"VIX low ({vix:.1f}) — complacent / risk-on")
        elif vix < 20:
            votes["vix"] = 0.5
            reasons.append(f"VIX contained ({vix:.1f})")
        elif vix < 25:
            votes["vix"] = -0.5
            reasons.append(f"VIX elevated ({vix:.1f})")
        elif vix < 30:
            votes["vix"] = -1.0
            reasons.append(f"VIX high ({vix:.1f}) — risk-off pressure")
        else:
            votes["vix"] = -1.5
            reasons.append(f"VIX stress ({vix:.1f}) — strong risk-off")
        # Trend confirmation
        vix_chg = _safe_float(proxies.get("vix_change_5d"))
        if vix_chg is not None:
            key_levels["vix_change_5d"] = round(vix_chg, 4)
            if vix_chg > 0.15:
                votes["vix"] = votes.get("vix", 0) - 0.5
                reasons.append(f"VIX rising sharply ({vix_chg:+.1%})")
            elif vix_chg < -0.15:
                votes["vix"] = votes.get("vix", 0) + 0.3
                reasons.append(f"VIX falling ({vix_chg:+.1%})")

    # --- Dollar pillar (stronger USD often headwind for risk assets) ---
    dollar_last = _safe_float(proxies.get("dollar_last"))
    dollar_chg = _safe_float(proxies.get("dollar_change_20d"))
    dollar_ma = _safe_float(proxies.get("dollar_ma50"))
    if dollar_last is not None or dollar_chg is not None:
        coverage += 1
        if dollar_last is not None:
            key_levels["dollar_last"] = dollar_last
            key_levels["dollar_ticker"] = proxies.get("dollar_ticker")
        if dollar_ma is not None:
            key_levels["dollar_ma50"] = dollar_ma
        if dollar_chg is not None:
            key_levels["dollar_change_20d"] = round(dollar_chg, 4)
            if dollar_chg > 0.02:
                votes["dollar"] = -1.0
                reasons.append(f"Dollar firm (+{dollar_chg:.1%} 20d) — risk headwind")
            elif dollar_chg < -0.02:
                votes["dollar"] = 1.0
                reasons.append(f"Dollar soft ({dollar_chg:+.1%} 20d) — risk support")
            else:
                votes["dollar"] = 0.0
                reasons.append(f"Dollar stable ({dollar_chg:+.1%} 20d)")
        elif dollar_last is not None and dollar_ma is not None:
            if dollar_last > dollar_ma * 1.01:
                votes["dollar"] = -0.5
                reasons.append("Dollar above MA50")
            elif dollar_last < dollar_ma * 0.99:
                votes["dollar"] = 0.5
                reasons.append("Dollar below MA50")
            else:
                votes["dollar"] = 0.0
                reasons.append("Dollar near MA50")

    # --- Rates pillar ---
    rates_last = _safe_float(proxies.get("rates_last"))
    rates_chg = _safe_float(proxies.get("rates_change_20d"))
    rates_is_yield = bool(proxies.get("rates_is_yield"))
    if rates_last is not None or rates_chg is not None:
        coverage += 1
        key_levels["rates_last"] = rates_last
        key_levels["rates_ticker"] = proxies.get("rates_ticker")
        key_levels["rates_is_yield"] = rates_is_yield
        if rates_chg is not None:
            key_levels["rates_change_20d"] = round(rates_chg, 4)
            # TLT up = yields down = typically risk-supportive; ^TNX up = opposite
            signed = -rates_chg if rates_is_yield else rates_chg
            # signed > 0 means "bond-friendly / lower yields" for risk
            if signed > 0.02:
                votes["rates"] = 1.0
                label = "yields easing" if rates_is_yield else "duration bid (TLT↑)"
                reasons.append(
                    f"Rates supportive ({rates_chg:+.1%} on {proxies.get('rates_ticker')}; {label})"
                )
            elif signed < -0.02:
                votes["rates"] = -1.0
                label = "yields rising" if rates_is_yield else "duration sell (TLT↓)"
                reasons.append(
                    f"Rates headwind ({rates_chg:+.1%} on {proxies.get('rates_ticker')}; {label})"
                )
            else:
                votes["rates"] = 0.0
                reasons.append(f"Rates stable ({rates_chg:+.1%})")

    # --- Equity benchmark trend pillar ---
    spy_last = _safe_float(proxies.get("spy_last"))
    ma50 = _safe_float(proxies.get("spy_ma50"))
    ma200 = _safe_float(proxies.get("spy_ma200"))
    vol20 = _safe_float(proxies.get("realized_vol_20d_ann"))
    if spy_last is not None:
        coverage += 1
        key_levels["spy_last"] = spy_last
        key_levels["benchmark"] = proxies.get("benchmark", "SPY")
        if ma50 is not None:
            key_levels["spy_ma50"] = ma50
        if ma200 is not None:
            key_levels["spy_ma200"] = ma200
        if vol20 is not None:
            key_levels["realized_vol_20d_ann"] = round(vol20, 4)

        trend_vote = 0.0
        if ma50 is not None and ma200 is not None:
            if spy_last > ma50 > ma200:
                trend_vote = 1.5
                reasons.append("Benchmark above MA50>MA200 (uptrend)")
            elif spy_last < ma50 < ma200:
                trend_vote = -1.5
                reasons.append("Benchmark below MA50<MA200 (downtrend)")
            elif spy_last > ma50:
                trend_vote = 0.5
                reasons.append("Benchmark above MA50 (short-term bid)")
            elif spy_last < ma50:
                trend_vote = -0.5
                reasons.append("Benchmark below MA50 (short-term pressure)")
            else:
                reasons.append("Benchmark mixed vs MAs")
        elif ma50 is not None:
            if spy_last > ma50:
                trend_vote = 0.5
                reasons.append("Benchmark above MA50")
            else:
                trend_vote = -0.5
                reasons.append("Benchmark below MA50")
        else:
            reasons.append("Insufficient MA history for benchmark trend")

        if vol20 is not None:
            if vol20 > 0.35:
                trend_vote -= 0.75
                reasons.append(f"Elevated realized vol ({vol20:.0%} ann)")
            elif vol20 < 0.15:
                trend_vote += 0.25
                reasons.append(f"Low realized vol ({vol20:.0%} ann)")

        spy_chg = _safe_float(proxies.get("spy_change_20d"))
        if spy_chg is not None:
            key_levels["spy_change_20d"] = round(spy_chg, 4)
            if spy_chg > 0.03:
                trend_vote += 0.25
            elif spy_chg < -0.03:
                trend_vote -= 0.25

        votes["equity"] = trend_vote

    score = float(sum(votes.values()))
    n_votes = len(votes)

    if coverage == 0 or n_votes == 0:
        return {
            "regime": "neutral",
            "direction": Direction.NEUTRAL,
            "confidence": 15.0,
            "macro_risk_level": "unknown",
            "raw_score": 0.0,
            "votes": votes,
            "reasons": [
                "Insufficient macro proxy coverage; emitting neutral with low confidence."
            ],
            "key_levels": key_levels,
            "coverage": coverage,
        }

    # Regime thresholds on aggregate vote
    if score >= 1.5:
        regime = "risk_on"
        direction = Direction.LONG
    elif score <= -1.5:
        regime = "risk_off"
        direction = Direction.SHORT
    else:
        regime = "neutral"
        direction = Direction.NEUTRAL

    # macro_risk_level from VIX + score
    if vix is not None and vix >= 30:
        macro_risk_level = "high"
    elif vix is not None and vix >= 25:
        macro_risk_level = "elevated"
    elif regime == "risk_off" or (vix is not None and vix >= 20):
        macro_risk_level = "moderate"
    elif regime == "risk_on" and (vix is None or vix < 18):
        macro_risk_level = "low"
    else:
        macro_risk_level = "moderate"

    # Confidence: |score| + coverage, penalize sparse data
    conf = 30.0 + abs(score) * 12.0 + coverage * 8.0
    if coverage < 2:
        conf = min(conf, 35.0)
        reasons.append(f"Sparse proxy coverage ({coverage}/4 pillars)")
    confidence = float(max(10.0, min(90.0, conf)))
    if regime == "neutral" and abs(score) < 0.5:
        confidence = min(confidence, 45.0)

    if not reasons:
        reasons.append("Macro proxies inconclusive")

    return {
        "regime": regime,
        "direction": direction,
        "confidence": confidence,
        "macro_risk_level": macro_risk_level,
        "raw_score": round(score, 3),
        "votes": {k: round(v, 3) for k, v in votes.items()},
        "reasons": reasons,
        "key_levels": key_levels,
        "coverage": coverage,
    }


def _sector_bias_for(ticker: str, regime: str) -> dict[str, Any]:
    """Map market regime → theme overlay for universe (high-beta growth) tickers."""
    theme = _THEME_TICKERS.get(ticker.upper())
    # Growth/high-beta themes amplify the market regime; unknowns get generic bias.
    if regime == "risk_on":
        bias = "pro_cyclical"
        tilt = 1.0
        note = "Risk-on: favor high-beta theme exposure"
    elif regime == "risk_off":
        bias = "defensive"
        tilt = -1.0
        note = "Risk-off: reduce/hedge high-beta theme exposure"
    else:
        bias = "balanced"
        tilt = 0.0
        note = "Neutral regime: no sector tilt"

    if theme:
        note = f"{note} ({theme})"
    return {
        "sector_bias": bias,
        "sector_tilt": tilt,
        "theme": theme,
        "bias_note": note,
    }


class MacroAgent(ResearchAgent):
    """Classify market regime from VIX / dollar / rates / SPY proxies.

    Emits one Signal per ticker (pipeline contract): market-wide regime applied
    as a sector-bias direction for universe names. Missing data -> neutral +
    low confidence; never crashes.
    """

    agent_id = "macro"

    def __init__(
        self,
        market_data: MarketData,
        proxies_fetcher: ProxiesFetcher | None = None,
    ):
        super().__init__(market_data)
        # Injected fetcher(benchmark) -> proxy dict; default uses market_data histories.
        self._proxies_fetcher = proxies_fetcher

    def _load_proxies(self, benchmark: str) -> tuple[dict[str, Any], str | None]:
        fetch_error: str | None = None
        try:
            if self._proxies_fetcher is not None:
                proxies = dict(self._proxies_fetcher(benchmark) or {})
            else:
                proxies = fetch_macro_proxies(self.market_data, benchmark=benchmark)
        except Exception as exc:  # noqa: BLE001
            proxies = {}
            fetch_error = str(exc)
        return proxies, fetch_error

    def analyze(self, ticker: str, *, benchmark: str = "SPY") -> Signal:
        proxies, fetch_error = self._load_proxies(benchmark)
        result = classify_regime(proxies)
        bias = _sector_bias_for(ticker, result["regime"])

        # Direction follows regime tilt for the ticker (market-wide applied per name).
        direction = result["direction"]
        confidence = float(result["confidence"])

        supporting: dict[str, Any] = {
            "regime": result["regime"],
            "macro_risk_level": result["macro_risk_level"],
            "sector_bias": bias["sector_bias"],
            "sector_tilt": bias["sector_tilt"],
            "theme": bias["theme"],
            "key_levels": result["key_levels"],
            "votes": result["votes"],
            "raw_score": result["raw_score"],
            "coverage": result["coverage"],
            "benchmark": benchmark.upper(),
            "proxies_errors": (proxies or {}).get("errors") or {},
            "fetch_error": fetch_error,
            "stub": False,
        }

        reason_parts = [
            f"Macro regime={result['regime']} (risk={result['macro_risk_level']})",
            bias["bias_note"],
        ]
        reason_parts.extend(result["reasons"][:6])
        if fetch_error:
            reason_parts.append(f"fetch_error={fetch_error}")

        return Signal(
            agent_id=self.agent_id,
            ticker=ticker,
            direction=direction,
            confidence=confidence,
            reasoning="; ".join(reason_parts),
            supporting_data=supporting,
        )
