"""Trade tickets and HOLD decisions produced by PortfolioManager."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TicketSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class TicketStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    FAILED = "failed"


class TradeTicket(BaseModel):
    ticker: str
    side: TicketSide
    qty: float = Field(gt=0)
    order_type: str = "market"
    rationale: str
    signal_ids: list[int] = Field(default_factory=list)
    status: TicketStatus = TicketStatus.PENDING
    limit_price: float | None = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )
    # Populated after DB insert
    id: int | None = None

    model_config = {"extra": "forbid"}


class HoldDecision(BaseModel):
    ticker: str | None = None
    rationale: str
    signal_ids: list[int] = Field(default_factory=list)
    data_snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )

    model_config = {"extra": "forbid"}
