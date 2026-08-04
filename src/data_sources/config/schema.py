from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    credentials: dict[str, Any] = Field(default_factory=dict)


class ConnectorConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    provider: str
    name: str | None = None
    auth: AuthConfig | None = None
    options: dict[str, Any] = Field(default_factory=dict)
