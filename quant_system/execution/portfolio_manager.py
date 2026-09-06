"""PortfolioManager — weighs research signals only; never invents ideas; enforces risk limits."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from quant_system.config.loader import AppConfig, RiskConfig
from quant_system.market_data.interface import MarketData
from quant_system.models.signal import Direction, Signal
from quant_system.models.ticket import HoldDecision, TicketSide, TradeTicket
from quant_system.tracker.api import Tracker


class PortfolioManager:
    """Consumes signals + weights -> TradeTicket or HOLD. Does not invent tickers/ideas."""

    def __init__(
        self,
        tracker: Tracker,
        market_data: MarketData,
        config: AppConfig,
    ):
        self.tracker = tracker
        self.market_data = market_data
        self.config = config
        self.risk: RiskConfig = config.risk

    def weigh_and_decide(
        self,
        signals: list[tuple[int, Signal]],
    ) -> list[TradeTicket | HoldDecision]:
        """Group by ticker, apply agent weights, emit tickets or holds."""
        blocked, reason = self.tracker.orders_blocked()
        if blocked:
            hold = HoldDecision(
                ticker=None,
                rationale=f"Orders blocked: {reason}",
                signal_ids=[sid for sid, _ in signals],
                data_snapshot={"halted": True, "reason": reason},
            )
            self.tracker.insert_hold(hold)
            return [hold]

        by_ticker: dict[str, list[tuple[int, Signal]]] = defaultdict(list)
        for sid, sig in signals:
            by_ticker[sig.ticker].append((sid, sig))

        weights = self.tracker.get_weights()
        results: list[TradeTicket | HoldDecision] = []

        cash = float(self.tracker.get_state("cash", str(self.risk.starting_equity)))
        equity = self._mark_equity(cash)
        positions = {p["ticker"]: p for p in self.tracker.list_positions()}

        # Risk checks before any ticket
        daily_pnl_pct, drawdown = self._risk_metrics(equity)
        if daily_pnl_pct <= -self.risk.max_daily_loss_pct:
            self.tracker.halt(
                f"Max daily loss breached: {daily_pnl_pct:.2%} <= -{self.risk.max_daily_loss_pct:.0%}",
                day_only=True,
            )
            hold = HoldDecision(
                rationale=f"Day halt: daily loss {daily_pnl_pct:.2%}",
                signal_ids=[sid for sid, _ in signals],
                data_snapshot={"daily_pnl_pct": daily_pnl_pct, "equity": equity},
            )
            self.tracker.insert_hold(hold)
            return [hold]

        if drawdown >= self.risk.max_drawdown_pct:
            self.tracker.halt(
                f"Max drawdown breached: {drawdown:.2%} >= {self.risk.max_drawdown_pct:.0%}"
            )
            self.tracker.insert_audit_flag(
                flag_type="max_drawdown",
                severity="critical",
                message=f"Drawdown {drawdown:.2%} halted trading",
            )
            hold = HoldDecision(
                rationale=f"Drawdown halt: {drawdown:.2%}",
                signal_ids=[sid for sid, _ in signals],
                data_snapshot={"drawdown": drawdown, "equity": equity},
            )
            self.tracker.insert_hold(hold)
            return [hold]

        for ticker, items in by_ticker.items():
            decision = self._decide_ticker(
                ticker, items, weights, equity, cash, positions
            )
            if isinstance(decision, TradeTicket):
                tid = self.tracker.insert_ticket(decision)
                decision.id = tid
            else:
                self.tracker.insert_hold(decision)
            results.append(decision)
        return results

    def _decide_ticker(
        self,
        ticker: str,
        items: list[tuple[int, Signal]],
        weights: dict[str, float],
        equity: float,
        cash: float,
        positions: dict[str, Any],
    ) -> TradeTicket | HoldDecision:
        signal_ids = [sid for sid, _ in items]
        # Weighted vote: long=+1, short=-1, neutral=0; scaled by confidence/100 * weight
        score = 0.0
        conf_acc = 0.0
        detail = []
        for sid, sig in items:
            w = float(weights.get(sig.agent_id, 0.0))
            # Floor/cap already stored; still clamp for safety
            w = max(self.risk.weight_floor, min(self.risk.weight_cap, w)) if w > 0 else 0.0
            direction_val = {
                Direction.LONG: 1.0,
                Direction.SHORT: -1.0,
                Direction.NEUTRAL: 0.0,
            }[sig.direction]
            contrib = direction_val * (sig.confidence / 100.0) * w
            score += contrib
            conf_acc += sig.confidence * w
            detail.append(
                {
                    "signal_id": sid,
                    "agent_id": sig.agent_id,
                    "direction": sig.direction.value,
                    "confidence": sig.confidence,
                    "weight": w,
                    "contrib": contrib,
                }
            )

        weight_sum = sum(float(weights.get(sig.agent_id, 0.0)) for _, sig in items) or 1.0
        blended_conf = conf_acc / weight_sum

        snapshot = {
            "weighted_score": score,
            "blended_confidence": blended_conf,
            "equity": equity,
            "cash": cash,
            "contributions": detail,
        }

        if abs(score) < 0.05 or blended_conf < self.risk.min_confidence_to_trade:
            return HoldDecision(
                ticker=ticker,
                rationale=(
                    f"HOLD {ticker}: score={score:.3f}, blended_conf={blended_conf:.1f} "
                    f"(min={self.risk.min_confidence_to_trade})"
                ),
                signal_ids=signal_ids,
                data_snapshot=snapshot,
            )

        try:
            price = self.market_data.last_price(ticker)
        except Exception as exc:  # noqa: BLE001
            return HoldDecision(
                ticker=ticker,
                rationale=f"HOLD {ticker}: cannot fetch price ({exc})",
                signal_ids=signal_ids,
                data_snapshot=snapshot,
            )

        snapshot["price"] = price
        pos = positions.get(ticker)
        pos_qty = float(pos["qty"]) if pos else 0.0
        pos_value = pos_qty * price
        max_pos_value = equity * self.risk.max_position_pct

        if score > 0:
            # Long / add
            room = max_pos_value - pos_value
            budget = min(room, cash * 0.95)
            if budget < self.risk.min_order_notional or price <= 0:
                return HoldDecision(
                    ticker=ticker,
                    rationale=(
                        f"HOLD {ticker}: insufficient budget/room "
                        f"(budget={budget:.2f}, max_pos={max_pos_value:.2f})"
                    ),
                    signal_ids=signal_ids,
                    data_snapshot=snapshot,
                )
            # Fractional shares OK for paper/sim
            qty = round(budget / price, 6)
            if qty * price < self.risk.min_order_notional:
                return HoldDecision(
                    ticker=ticker,
                    rationale=f"HOLD {ticker}: qty too small for min notional",
                    signal_ids=signal_ids,
                    data_snapshot=snapshot,
                )
            # Cap so position won't exceed 20% after fill
            if (pos_value + qty * price) > max_pos_value + 1e-6:
                qty = max(0.0, (max_pos_value - pos_value) / price)
                qty = round(qty, 6)
            if qty <= 0:
                return HoldDecision(
                    ticker=ticker,
                    rationale=f"HOLD {ticker}: at max position 20%",
                    signal_ids=signal_ids,
                    data_snapshot=snapshot,
                )
            return TradeTicket(
                ticker=ticker,
                side=TicketSide.BUY,
                qty=qty,
                rationale=(
                    f"BUY {qty} {ticker} @~{price:.2f}; weighted_score={score:.3f}; "
                    f"blended_conf={blended_conf:.1f}; max_pos={self.risk.max_position_pct:.0%}"
                ),
                signal_ids=signal_ids,
            )

        # score < 0 -> reduce / sell (short only if allow_short and no long)
        if pos_qty > 0:
            # Sell portion scaled by |score|
            sell_frac = min(1.0, abs(score) / 0.5)
            qty = round(pos_qty * sell_frac, 6)
            if qty <= 0:
                return HoldDecision(
                    ticker=ticker,
                    rationale=f"HOLD {ticker}: bearish but nothing to sell",
                    signal_ids=signal_ids,
                    data_snapshot=snapshot,
                )
            return TradeTicket(
                ticker=ticker,
                side=TicketSide.SELL,
                qty=qty,
                rationale=(
                    f"SELL {qty} {ticker} @~{price:.2f}; weighted_score={score:.3f}; "
                    f"reduce risk"
                ),
                signal_ids=signal_ids,
            )

        if self.risk.allow_short:
            # Not opening shorts on $100 account by default
            target = min(max_pos_value, cash * 0.5)
            qty = round(target / price, 6) if price > 0 else 0.0
            if qty * price < self.risk.min_order_notional:
                return HoldDecision(
                    ticker=ticker,
                    rationale=f"HOLD {ticker}: short size below min notional",
                    signal_ids=signal_ids,
                    data_snapshot=snapshot,
                )
            return TradeTicket(
                ticker=ticker,
                side=TicketSide.SELL,
                qty=qty,
                rationale=f"SHORT {qty} {ticker} (allow_short=true); score={score:.3f}",
                signal_ids=signal_ids,
            )

        return HoldDecision(
            ticker=ticker,
            rationale=f"HOLD {ticker}: bearish score but shorts disabled / no long to exit",
            signal_ids=signal_ids,
            data_snapshot=snapshot,
        )

    def _mark_equity(self, cash: float) -> float:
        equity = cash
        for p in self.tracker.list_positions():
            try:
                px = self.market_data.last_price(p["ticker"])
            except Exception:  # noqa: BLE001
                px = float(p["avg_cost"])
            equity += float(p["qty"]) * px
        return equity

    def _risk_metrics(self, equity: float) -> tuple[float, float]:
        day_start = float(self.tracker.get_state("day_start_equity", str(self.risk.starting_equity)))
        peak = float(self.tracker.get_state("peak_equity", str(self.risk.starting_equity)))
        if equity > peak:
            peak = equity
            self.tracker.set_state("peak_equity", f"{peak:.6f}")
        daily_pnl_pct = (equity - day_start) / day_start if day_start else 0.0
        drawdown = (peak - equity) / peak if peak else 0.0
        return daily_pnl_pct, drawdown
