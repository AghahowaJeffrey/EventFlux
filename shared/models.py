"""
EventFlux — Shared Event Schema (single source of truth).

Both the ingestion API and the batch worker import from here.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


class Event(BaseModel):
    """A single telemetry event submitted by a client application."""

    event_type: str = Field(..., min_length=1, max_length=128)
    actor_id: str = Field(..., min_length=1, max_length=256)
    source: str = Field(..., min_length=1, max_length=64)
    timestamp: datetime
    attributes: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}

    @field_validator("event_type", "actor_id", "source", mode="before")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        if isinstance(v, str):
            return v.strip()
        return v


class BulkEventRequest(BaseModel):
    """Payload for POST /v1/events/bulk."""

    request_id: UUID = Field(default_factory=uuid4, description="Client-supplied or auto-generated trace ID")
    events: list[Event] = Field(..., min_length=1)

    model_config = {"extra": "forbid"}


class EventBulkResponse(BaseModel):
    """Response body for 202 Accepted from POST /v1/events/bulk."""

    request_id: str
    queued: int
    stream_len: int | None = None
    mode: str = "queue"  # "queue" | "bypass-queue"
