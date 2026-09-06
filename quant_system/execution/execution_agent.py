"""ExecutionAgent — Alpaca paper when keys present; otherwise dry-run sim fills.

Hard rules: paper only; no withdraw/transfer/account-settings mutations; respect halt.
"""

from __future__ import annotations

from typing import Any

from quant_system.config.loader import alpaca_credentials
from quant_system.market_data.interface import MarketData
from quant_system.models.ticket import TicketStatus, TradeTicket
from quant_system.tracker.api import Tracker


class ExecutionAgent:
    def __init__(self, tracker: Tracker, market_data: MarketData):
        self.tracker = tracker
        self.market_data = market_data
        self.creds = alpaca_credentials()
        self._alpaca = None
        if self.creds["configured"]:
            self._alpaca = self._init_alpaca()

    def _init_alpaca(self) -> Any:
        try:
            from alpaca.trading.client import TradingClient

            return TradingClient(
                self.creds["api_key"],
                self.creds["secret_key"],
                paper=bool(self.creds["paper"]),
            )
        except Exception as exc:  # noqa: BLE001
            self.tracker.log_decision(
                decision_type="execution_init",
                actor="execution_agent",
                reasoning=f"Alpaca init failed, falling back to dry-run: {exc}",
                payload={"error": str(exc)},
            )
            return None

    @property
    def mode(self) -> str:
        return "alpaca" if self._alpaca is not None else "dry_run"

    def execute(self, ticket: TradeTicket) -> dict[str, Any] | None:
        blocked, reason = self.tracker.orders_blocked()
        if blocked:
            if ticket.id is not None:
                self.tracker.update_ticket_status(ticket.id, TicketStatus.BLOCKED)
            self.tracker.log_decision(
                decision_type="blocked",
                actor="execution_agent",
                ticker=ticket.ticker,
                reasoning=f"Order blocked by kill switch: {reason}",
                payload={"ticket": ticket.model_dump(), "reason": reason},
            )
            return None

        if ticket.id is None:
            raise ValueError("Ticket must be persisted (id) before execution")

        # Never mutate account settings / withdraw / transfer — marketable orders only.
        price = self.market_data.last_price(ticket.ticker)
        side = ticket.side.value if hasattr(ticket.side, "value") else str(ticket.side)

        if self._alpaca is not None:
            return self._execute_alpaca(ticket, price, side)
        return self._execute_dry_run(ticket, price, side)

    def _execute_dry_run(self, ticket: TradeTicket, price: float, side: str) -> dict[str, Any]:
        self.tracker.update_ticket_status(ticket.id, TicketStatus.SUBMITTED)
        fill_id = self.tracker.insert_fill(
            ticket_id=ticket.id,
            ticker=ticket.ticker,
            side=side,
            qty=float(ticket.qty),
            price=float(price),
            mode="dry_run",
        )
        # Refresh equity snapshot
        cash = float(self.tracker.get_state("cash", "100"))
        positions = self.tracker.list_positions()
        equity = cash
        for p in positions:
            try:
                px = self.market_data.last_price(p["ticker"])
            except Exception:  # noqa: BLE001
                px = float(p["avg_cost"])
            equity += float(p["qty"]) * px
        peak = max(float(self.tracker.get_state("peak_equity", "100")), equity)
        day_start = float(self.tracker.get_state("day_start_equity", "100"))
        self.tracker.snapshot_portfolio(
            equity=equity,
            cash=cash,
            positions=positions,
            daily_pnl=equity - day_start,
            drawdown=(peak - equity) / peak if peak else 0.0,
            peak_equity=peak,
        )
        return {
            "mode": "dry_run",
            "fill_id": fill_id,
            "ticket_id": ticket.id,
            "ticker": ticket.ticker,
            "side": side,
            "qty": ticket.qty,
            "price": price,
        }

    def _execute_alpaca(self, ticket: TradeTicket, price: float, side: str) -> dict[str, Any]:
        """Submit paper market order only. No account mutations."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        assert self._alpaca is not None
        if not self.creds["paper"]:
            # Hard refuse live trading
            self.tracker.update_ticket_status(ticket.id, TicketStatus.BLOCKED)
            self.tracker.log_decision(
                decision_type="blocked",
                actor="execution_agent",
                ticker=ticket.ticker,
                reasoning="Refusing non-paper Alpaca credentials (paper only)",
                payload={"ticket_id": ticket.id},
            )
            return {"mode": "blocked", "reason": "paper_only"}

        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
        req = MarketOrderRequest(
            symbol=ticket.ticker,
            qty=float(ticket.qty),
            side=order_side,
            time_in_force=TimeInForce.DAY,
        )
        self.tracker.update_ticket_status(ticket.id, TicketStatus.SUBMITTED)
        order = self._alpaca.submit_order(req)
        # Record sim-mark fill locally for Tracker equity; Alpaca is source of broker truth
        fill_id = self.tracker.insert_fill(
            ticket_id=ticket.id,
            ticker=ticket.ticker,
            side=side,
            qty=float(ticket.qty),
            price=float(price),
            mode="alpaca",
        )
        self.tracker.log_decision(
            decision_type="alpaca_order",
            actor="execution_agent",
            ticker=ticket.ticker,
            reasoning="Submitted Alpaca paper market order",
            payload={"order_id": str(getattr(order, "id", "")), "ticket_id": ticket.id},
        )
        return {
            "mode": "alpaca",
            "fill_id": fill_id,
            "ticket_id": ticket.id,
            "order_id": str(getattr(order, "id", "")),
            "price": price,
        }
