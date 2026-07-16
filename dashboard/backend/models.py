"""Request/response schemas for the dashboard API.

Kept deliberately small: the dashboard is a thin control/monitor layer over
`mcbot.Client`, so these mostly mirror the client's constructor and the shape
of the events we fan out over the WebSocket.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class CreateBotRequest(BaseModel):
    host: str
    port: int = 25565
    username: str = "Bot"
    version: str = "auto"
    # Advertise a protocol number newer than the vendored schema (see
    # Client.advertise_protocol). Optional; defaults to the schema's own.
    advertise_protocol: Optional[int] = None
    # Automatically reconnect (with backoff) when the connection drops.
    auto_reconnect: bool = True


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)


class BotStatus(BaseModel):
    id: str
    username: str
    host: str
    port: int
    version: str
    # created | connecting | configuring | play | reconnecting | disconnected | error
    state: str
    position: Optional[dict] = None
    created_at: float
    connected_at: Optional[float] = None
    last_error: Optional[str] = None
    auto_reconnect: bool = True
    reconnect_attempts: int = 0


class Event(BaseModel):
    """One live event fanned out to WebSocket subscribers."""
    type: str
    bot_id: str
    ts: float
    data: Any = None
