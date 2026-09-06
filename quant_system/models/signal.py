"""Frozen Signal model — research agents emit only this shape."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class Signal(BaseModel):
    """Frozen research signal contract. Do not extend without migration."""

    agent_id: str
    ticker: str
    direction: Direction
    confidence: float = Field(ge=0, le=100)
    reasoning: str
    supporting_data: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )

    @field_validator("ticker")
    @classmethod
    def upper_ticker(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("created_at")
    @classmethod
    def require_utc_iso(cls, v: str) -> str:
        if not v:
            raise ValueError("created_at required")
        return v

    model_config = {"extra": "forbid"}
