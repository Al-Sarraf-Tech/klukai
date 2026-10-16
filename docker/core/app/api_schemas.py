"""Validated wire request models, independent of HTTP route registration."""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field, field_validator
from .passwords import MAX_PASSWORD_BYTES


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=128)


class TTSRequest(BaseModel):
    text: str = Field(min_length=1)
    language: str = "en"


class STTRequest(BaseModel):
    # Same ceiling as the voice service's own STTRequest (~6 MB of audio once
    # decoded): refuse at validation rather than proxy a multi-MB body to it.
    audio: str = Field(min_length=1, max_length=8_000_000)


class ImageGenRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    request_id: str | None = Field(default=None, max_length=36)


class GiftRequest(BaseModel):
    gift: str = Field(min_length=1)


class CostumeRequest(BaseModel):
    costume: str


class PushSubscription(BaseModel):
    endpoint: str
    keys: dict[str, Any]


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def bcrypt_byte_limit(cls, value: str) -> str:
        if len(value.encode()) > MAX_PASSWORD_BYTES:
            raise ValueError("Password must be at most 72 UTF-8 bytes")
        return value


class TributeRequest(BaseModel):
    """Body for POST /api/tribute — Commander's heartfelt message to Klukai."""

    text: str = Field(min_length=20, max_length=1000)
    make_crown_jewel: bool = True


class AccountDeactivateRequest(BaseModel):
    """Body for POST /api/account/deactivate — soft delete. SACRED chat
    data is preserved per CLAUDE.md absolute rule."""

    confirm: str = Field(pattern=r"^DEACTIVATE$")
