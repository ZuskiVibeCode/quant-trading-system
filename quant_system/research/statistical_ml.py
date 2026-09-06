"""StatisticalMLAgent — lightweight statistical features (stub-ready, basic live)."""

from __future__ import annotations

import math

import numpy as np

from quant_system.market_data.interface import MarketData
from quant_system.models.signal import Direction, Signal
from quant_system.research.base import ResearchAgent


class StatisticalMLAgent(ResearchAgent):
    """Simple z-score / mean-reversion + momentum blend.

    TODO: train OOS models (ridge/logistic) on frozen feature store; never fit in-sample for weights.
    """

    agent_id = "statistical_ml"

    def analyze(self, ticker: str, *, benchmark: str = "SPY") -> Signal:
        df = self.market_data.history(ticker, period="1y", interval="1d")
        close = df["Close"]
        rets = close.pct_change().dropna()
        if len(rets) < 30:
            return Signal(
                agent_id=self.agent_id,
                ticker=ticker,
                direction=Direction.NEUTRAL,
                confidence=5.0,
                reasoning="Insufficient history for statistical features",
                supporting_data={"bars": len(df)},
            )

        lookback = min(60, len(rets))
        window = rets.iloc[-lookback:]
        mu = float(window.mean())
        sigma = float(window.std(ddof=1)) or 1e-9
        last_ret = float(window.iloc[-1])
        z = (last_ret - mu) / sigma

        # 20d momentum
        mom20 = float(close.iloc[-1] / close.iloc[-21] - 1) if len(close) >= 21 else 0.0
        vol20 = float(rets.iloc[-20:].std(ddof=1)) if len(rets) >= 20 else sigma

        # Blend: mild mean-reversion on extreme z, else follow mom
        score = 0.0
        reasons = []
        if z <= -1.5:
            score += 12
            reasons.append(f"oversold z={z:.2f}")
        elif z >= 1.5:
            score -= 12
            reasons.append(f"overbought z={z:.2f}")
        else:
            reasons.append(f"z={z:.2f} near mean")

        if mom20 > 0.05:
            score += 10
            reasons.append(f"20d mom {mom20:+.1%}")
        elif mom20 < -0.05:
            score -= 10
            reasons.append(f"20d mom {mom20:+.1%}")

        if score >= 10:
            direction = Direction.LONG
        elif score <= -10:
            direction = Direction.SHORT
        else:
            direction = Direction.NEUTRAL

        confidence = float(np.clip(45 + abs(score) * 1.5, 5, 85))
        if math.isnan(confidence):
            confidence = 10.0

        return Signal(
            agent_id=self.agent_id,
            ticker=ticker,
            direction=direction,
            confidence=confidence,
            reasoning="; ".join(reasons),
            supporting_data={
                "z_last_ret": z,
                "mu_ret": mu,
                "sigma_ret": sigma,
                "mom20": mom20,
                "vol20": vol20,
                "raw_score": score,
                "todos": [
                    "OOS supervised model on feature store",
                    "Walk-forward validation only",
                ],
            },
        )
