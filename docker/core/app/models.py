"""Pydantic models for companion-core."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def new_id() -> str:
    return str(uuid.uuid4())


# ── Enums ────────────────────────────────────────────────────────────────────

class Role(str, Enum):
    user = "user"
    assistant = "assistant"
    system = "system"


class Mood(str, Enum):
    neutral = "neutral"
    happy = "happy"
    curious = "curious"
    concerned = "concerned"
    playful = "playful"
    thoughtful = "thoughtful"
    excited = "excited"
    tired = "tired"
    annoyed = "annoyed"


# ── WebSocket protocol ──────────────────────────────────────────────────────

class WSMessageIn(BaseModel):
    type: str  # message, voice_start, voice_chunk, voice_end, typing
    content: str | None = None
    attachments: list[str] = Field(default_factory=list)
    audio: str | None = None  # base64 opus


class WSMessageOut(BaseModel):
    type: str  # token, done, thinking, tool_use, voice_audio, mood, proactive
    text: str | None = None
    message_id: str | None = None
    model: str | None = None
    tool: str | None = None
    status: str | None = None
    audio: str | None = None
    final: bool | None = None
    mood: str | None = None
    message: str | None = None


# ── Database models ─────────────────────────────────────────────────────────

class Conversation(BaseModel):
    id: str = Field(default_factory=new_id)
    started_at: datetime = Field(default_factory=datetime.now)
    ended_at: datetime | None = None
    summary: str | None = None
    mood_start: str = "neutral"
    mood_end: str = "neutral"
    turn_count: int = 0
    model_used: str = ""


class Message(BaseModel):
    id: str = Field(default_factory=new_id)
    conversation_id: str
    role: Role
    content: str
    content_type: str = "text"
    mood: str = "neutral"
    tool_calls: dict[str, Any] | None = None
    tokens_used: int = 0
    model: str = ""
    latency_ms: int | None = None
    created_at: datetime = Field(default_factory=datetime.now)


class Episode(BaseModel):
    id: str = Field(default_factory=new_id)
    conversation_id: str | None = None
    summary: str
    keywords: list[str] = Field(default_factory=list)
    emotion_tags: list[str] = Field(default_factory=list)
    importance: float = 0.5
    embedding_id: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)


# ── LLM routing ─────────────────────────────────────────────────────────────

class LLMConfig(BaseModel):
    provider: str  # "lmstudio" or "anthropic"
    model: str
    base_url: str | None = None
    max_tokens: int = 1024
    temperature: float = 0.8


# ── Session state (Redis) ───────────────────────────────────────────────────

class SessionState(BaseModel):
    conversation_id: str
    turns: list[dict[str, str]] = Field(default_factory=list)
    mood: str = "neutral"
    active_topic: str | None = None
    turn_count: int = 0
    last_activity: datetime = Field(default_factory=datetime.now)
