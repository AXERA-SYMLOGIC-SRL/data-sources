from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ItemType(StrEnum):
    FILE = "file"
    FOLDER = "folder"


class HashAlgorithm(StrEnum):
    MD5 = "md5"
    SHA1 = "sha1"
    SHA256 = "sha256"
    CRC32 = "crc32"
    QUICK_XOR = "quick_xor"
    ETAG = "etag"


class Hash(BaseModel):
    algorithm: HashAlgorithm
    value: str


class Item(BaseModel):
    id: str
    name: str
    type: ItemType
    path: str | None = None
    parent_id: str | None = None
    size: int | None = None
    mime_type: str | None = None
    hashes: list[Hash] = Field(default_factory=list)
    created_at: datetime | None = None
    modified_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PermissionRole(StrEnum):
    OWNER = "owner"
    WRITER = "writer"
    COMMENTER = "commenter"
    READER = "reader"


class PermissionPrincipalType(StrEnum):
    USER = "user"
    GROUP = "group"
    DOMAIN = "domain"
    ANYONE = "anyone"


class Permission(BaseModel):
    principal: str
    principal_type: PermissionPrincipalType
    role: PermissionRole


class ChangeType(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"


class SyncCursor(BaseModel):
    token: str | None = None


class Change(BaseModel):
    item_id: str
    type: ChangeType
    item: Item | None = None
    timestamp: datetime | None = None
    #: Cursor reflecting sync progress once this change (and everything before it in the
    #: same `sync()` call) has been durably processed. `None` when the provider's delta
    #: API doesn't expose per-item resume points (e.g. only a per-page token).
    cursor: SyncCursor | None = None


class Subscription(BaseModel):
    """A provider's change-notification subscription (e.g. a Graph webhook)."""

    id: str
    resource: str
    notification_url: str
    expiration: datetime
    client_state: str | None = None
