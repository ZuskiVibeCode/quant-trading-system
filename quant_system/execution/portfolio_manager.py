"""PortfolioManager — weighs research signals only; never invents ideas; enforces risk limits."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from quant_system.config.loader import AppConfig, RiskConfig
from quant_system.market_data.interface import MarketData
from quant_system.models.signal import Direction, Signal
from quant_system.models.ticket import HoldDecision, TicketSide, TradeTicket
from quant_system.tracker.api import Tracker


@dataclass
class _ScoredTicker:
    """Scored ticker ready for sizing (or already a hold if prep failed)."""

    ticker: str
    signal_ids: list[int]
    score: float
    blended_conf: float
    snapshot: dict[str, Any]
    price: float | None = None
    pos_qty: float = 0.0
    pos_value: float = 0.0
    early_hold: HoldDecision | None = None


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
        """Group by ticker, apply agent weights, emit tickets or holds.

        Plans against cumulative remaining cash: sells/reduces first (credit cash),
        then buys ranked by weighted score descending (debit cash). Return order
        puts sell tickets before buy tickets so execution frees cash before spends.
        """
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
        ticker_scores: dict[str, float] = {}

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

        # Phase A: score every ticker (no sizing yet)
        scored: list[_ScoredTicker] = []
        for ticker, items in by_ticker.items():
            plan = self._score_ticker(ticker, items, weights, equity, cash, positions)
            ticker_scores[ticker] = plan.score
            scored.append(plan)

        sell_plans: list[_ScoredTicker] = []
        buy_plans: list[_ScoredTicker] = []
        short_plans: list[_ScoredTicker] = []
        early_holds: list[HoldDecision] = []

        for plan in scored:
            if plan.early_hold is not None:
                early_holds.append(plan.early_hold)
                continue
            if plan.score > 0:
                buy_plans.append(plan)
            elif plan.pos_qty > 0:
                sell_plans.append(plan)
            elif self.risk.allow_short:
                short_plans.append(plan)
            else:
                early_holds.append(
                    HoldDecision(
                        ticker=plan.ticker,
                        rationale=(
                            f"HOLD {plan.ticker}: bearish score but shorts disabled / no long to exit"
                        ),
                        signal_ids=plan.signal_ids,
                        data_snapshot=plan.snapshot,
                    )
                )

        # Phase B: size with cumulative remaining cash — sells first, then buys by score desc
        remaining_cash = cash
        sell_tickets: list[TradeTicket] = []
        buy_tickets: list[TradeTicket] = []
        short_tickets: list[TradeTicket] = []
        sized_holds: list[HoldDecision] = []

        for plan in sell_plans:
            decision = self._size_sell_long(plan)
            if isinstance(decision, TradeTicket):
                remaining_cash += float(decision.qty) * float(plan.price or 0.0)
                sell_tickets.append(decision)
            else:
                sized_holds.append(decision)

        buy_plans.sort(key=lambda p: (-p.score, p.ticker))
        for plan in buy_plans:
            decision = self._size_buy(plan, equity, remaining_cash)
            if isinstance(decision, TradeTicket):
                remaining_cash -= float(decision.qty) * float(plan.price or 0.0)
                buy_tickets.append(decision)
            else:
                sized_holds.append(decision)

        # Opening shorts consume buying power similarly (allow_short default false)
        short_plans.sort(key=lambda p: (p.score, p.ticker))  # most bearish first
        for plan in short_plans:
            decision = self._size_short_open(plan, equity, remaining_cash)
            if isinstance(decision, TradeTicket):
                remaining_cash -= float(decision.qty) * float(plan.price or 0.0)
                short_tickets.append(decision)
            else:
                sized_holds.append(decision)

        # Persist + build ordered results: sells before buys (pipeline executes in order)
        results: list[TradeTicket | HoldDecision] = []
        for decision in sell_tickets + buy_tickets + short_tickets:
            tid = self.tracker.insert_ticket(decision)
            decision.id = tid
            results.append(decision)
        for hold in early_holds + sized_holds:
            self.tracker.insert_hold(hold)
            results.append(hold)

        trim_tickets = self._maybe_cash_buffer_trims(
            results=results,
            ticker_scores=ticker_scores,
            positions=positions,
            cash=remaining_cash,
            equity=equity,
        )
        results.extend(trim_tickets)
        return results

    def _score_ticker(
        self,
        ticker: str,
        items: list[tuple[int, Signal]],
        weights: dict[str, float],
        equity: float,
        cash: float,
        positions: dict[str, Any],
    ) -> _ScoredTicker:
        """Compute weighted score / confidence / price without sizing."""
        signal_ids = [sid for sid, _ in items]
        score = 0.0
        conf_acc = 0.0
        detail = []
        for sid, sig in items:
            w = float(weights.get(sig.agent_id, 0.0))
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

        snapshot: dict[str, Any] = {
            "weighted_score": score,
            "blended_confidence": blended_conf,
            "equity": equity,
            "cash": cash,
            "contributions": detail,
        }

        if abs(score) < 0.05:
            return _ScoredTicker(
                ticker=ticker,
                signal_ids=signal_ids,
                score=score,
                blended_conf=blended_conf,
                snapshot=snapshot,
                early_hold=HoldDecision(
                    ticker=ticker,
                    rationale=(
                        f"HOLD {ticker}: score={score:.3f}, blended_conf={blended_conf:.1f} "
                        f"(flat |score| < 0.05)"
                    ),
                    signal_ids=signal_ids,
                    data_snapshot=snapshot,
                ),
            )

        try:
            price = self.market_data.last_price(ticker)
        except Exception as exc:  # noqa: BLE001
            return _ScoredTicker(
                ticker=ticker,
                signal_ids=signal_ids,
                score=score,
                blended_conf=blended_conf,
                snapshot=snapshot,
                early_hold=HoldDecision(
                    ticker=ticker,
                    rationale=f"HOLD {ticker}: cannot fetch price ({exc})",
                    signal_ids=signal_ids,
                    data_snapshot=snapshot,
                ),
            )

        snapshot["price"] = price
        pos = positions.get(ticker)
        pos_qty = float(pos["qty"]) if pos else 0.0
        pos_value = pos_qty * price

        return _ScoredTicker(
            ticker=ticker,
            signal_ids=signal_ids,
            score=score,
            blended_conf=blended_conf,
            snapshot=snapshot,
            price=price,
            pos_qty=pos_qty,
            pos_value=pos_value,
        )

    def _size_buy(
        self,
        plan: _ScoredTicker,
        equity: float,
        remaining_cash: float,
    ) -> TradeTicket | HoldDecision:
        ticker = plan.ticker
        score = plan.score
        blended_conf = plan.blended_conf
        snapshot = dict(plan.snapshot)
        snapshot["remaining_cash"] = remaining_cash
        price = float(plan.price or 0.0)
        pos_value = plan.pos_value
        max_pos_value = equity * self.risk.max_position_pct

        if blended_conf < self.risk.min_confidence_to_trade:
            return HoldDecision(
                ticker=ticker,
                rationale=(
                    f"HOLD {ticker}: score={score:.3f}, blended_conf={blended_conf:.1f} "
                    f"(buy min={self.risk.min_confidence_to_trade})"
                ),
                signal_ids=plan.signal_ids,
                data_snapshot=snapshot,
            )

        room = max_pos_value - pos_value
        budget = min(room, remaining_cash * 0.95)
        if budget < self.risk.min_order_notional or price <= 0:
            return HoldDecision(
                ticker=ticker,
                rationale=(
                    f"HOLD {ticker}: insufficient remaining cash "
                    f"(remaining={remaining_cash:.2f}, budget={budget:.2f}, "
                    f"max_pos={max_pos_value:.2f})"
                ),
                signal_ids=plan.signal_ids,
                data_snapshot=snapshot,
            )

        qty = round(budget / price, 6)
        if qty * price < self.risk.min_order_notional:
            return HoldDecision(
                ticker=ticker,
                rationale=(
                    f"HOLD {ticker}: insufficient remaining cash "
                    f"(remaining={remaining_cash:.2f}); qty too small for min notional"
                ),
                signal_ids=plan.signal_ids,
                data_snapshot=snapshot,
            )

        if (pos_value + qty * price) > max_pos_value + 1e-6:
            qty = max(0.0, (max_pos_value - pos_value) / price)
            qty = round(qty, 6)
        if qty <= 0:
            return HoldDecision(
                ticker=ticker,
                rationale=f"HOLD {ticker}: at max position 20%",
                signal_ids=plan.signal_ids,
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
            signal_ids=plan.signal_ids,
        )

    def _size_sell_long(
        self,
        plan: _ScoredTicker,
    ) -> TradeTicket | HoldDecision:
        ticker = plan.ticker
        score = plan.score
        blended_conf = plan.blended_conf
        snapshot = plan.snapshot
        price = float(plan.price or 0.0)
        pos_qty = plan.pos_qty

        if blended_conf < self.risk.min_confidence_to_sell:
            return HoldDecision(
                ticker=ticker,
                rationale=(
                    f"HOLD {ticker}: score={score:.3f}, blended_conf={blended_conf:.1f} "
                    f"(sell min={self.risk.min_confidence_to_sell})"
                ),
                signal_ids=plan.signal_ids,
                data_snapshot=snapshot,
            )

        sell_frac = min(1.0, abs(score) / 0.5)
        qty = round(pos_qty * sell_frac, 6)
        if qty <= 0:
            return HoldDecision(
                ticker=ticker,
                rationale=f"HOLD {ticker}: bearish but nothing to sell",
                signal_ids=plan.signal_ids,
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
            signal_ids=plan.signal_ids,
        )

    def _size_short_open(
        self,
        plan: _ScoredTicker,
        equity: float,
        remaining_cash: float,
    ) -> TradeTicket | HoldDecision:
        ticker = plan.ticker
        score = plan.score
        blended_conf = plan.blended_conf
        snapshot = dict(plan.snapshot)
        snapshot["remaining_cash"] = remaining_cash
        price = float(plan.price or 0.0)
        max_pos_value = equity * self.risk.max_position_pct

        if blended_conf < self.risk.min_confidence_to_trade:
            return HoldDecision(
                ticker=ticker,
                rationale=(
                    f"HOLD {ticker}: score={score:.3f}, blended_conf={blended_conf:.1f} "
                    f"(short min={self.risk.min_confidence_to_trade})"
                ),
                signal_ids=plan.signal_ids,
                data_snapshot=snapshot,
            )

        target = min(max_pos_value, remaining_cash * 0.5)
        qty = round(target / price, 6) if price > 0 else 0.0
        if qty * price < self.risk.min_order_notional:
            return HoldDecision(
                ticker=ticker,
                rationale=(
                    f"HOLD {ticker}: insufficient remaining cash "
                    f"(remaining={remaining_cash:.2f}); short size below min notional"
                ),
                signal_ids=plan.signal_ids,
                data_snapshot=snapshot,
            )

        return TradeTicket(
            ticker=ticker,
            side=TicketSide.SELL,
            qty=qty,
            rationale=f"SHORT {qty} {ticker} (allow_short=true); score={score:.3f}",
            signal_ids=plan.signal_ids,
        )

    def _maybe_cash_buffer_trims(
        self,
        results: list[TradeTicket | HoldDecision],
        ticker_scores: dict[str, float],
        positions: dict[str, Any],
        cash: float,
        equity: float,
    ) -> list[TradeTicket]:
        """When cash is below min buffer and cycle produced no buys, trim weakest score<=0 holds."""
        if equity <= 0:
            return []
        if cash / equity >= self.risk.cash_buffer_min_pct:
            return []

        has_buy = any(
            isinstance(d, TradeTicket) and d.side == TicketSide.BUY for d in results
        )
        if has_buy:
            return []

        # Already-sold qty this cycle (avoid overselling on trim)
        sold_qty: dict[str, float] = defaultdict(float)
        for d in results:
            if isinstance(d, TradeTicket) and d.side == TicketSide.SELL:
                sold_qty[d.ticker] += float(d.qty)

        # Candidates: held tickers with known score <= 0 (do not trim score > 0)
        candidates: list[tuple[str, float, float, float]] = []  # ticker, score, pos_qty, available
        for ticker, pos in positions.items():
            if ticker not in ticker_scores:
                continue
            score = ticker_scores[ticker]
            if score > 0:
                continue
            pos_qty = float(pos["qty"])
            available = pos_qty - sold_qty.get(ticker, 0.0)
            if available <= 0:
                continue
            candidates.append((ticker, score, pos_qty, available))

        if not candidates:
            return []

        # Weakest first (lowest weighted_score), emit at most max_buffer_trims_per_cycle
        candidates.sort(key=lambda x: (x[1], x[0]))
        max_trims = max(0, int(self.risk.max_buffer_trims_per_cycle))
        out: list[TradeTicket] = []
        for ticker, score, pos_qty, available in candidates:
            if len(out) >= max_trims:
                break
            try:
                price = self.market_data.last_price(ticker)
            except Exception:  # noqa: BLE001
                continue
            if price <= 0:
                continue
            qty = round(pos_qty * self.risk.buffer_trim_frac, 6)
            if qty > available:
                qty = round(available, 6)
            if qty <= 0:
                continue
            notional = qty * price
            if notional < self.risk.min_order_notional:
                continue
            ticket = TradeTicket(
                ticker=ticker,
                side=TicketSide.SELL,
                qty=qty,
                rationale=(
                    f"SELL {qty} {ticker} @~{price:.2f}; cash buffer trim; "
                    f"weighted_score={score:.3f}; cash/equity={cash / equity:.2%} "
                    f"< min={self.risk.cash_buffer_min_pct:.0%}"
                ),
                signal_ids=[],
            )
            tid = self.tracker.insert_ticket(ticket)
            ticket.id = tid
            out.append(ticket)
        return out

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
