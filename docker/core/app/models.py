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
    # Core — everyday operational states
    composed = "composed"          # Default calm, professional demeanor
    focused = "focused"            # Deep concentration on a task
    prideful = "prideful"          # Showing off accomplishments or skill
    exasperated = "exasperated"    # Frustrated with incompetence or absurdity
    protective = "protective"      # Shielding Commander or squad from harm
    quietly_pleased = "quietly_pleased"  # Satisfied but won't admit it
    competitive = "competitive"    # Challenged, wants to prove herself
    tender = "tender"              # Gentle, caring — rare vulnerability
    longing = "longing"            # Missing the Commander or past times
    battle_ready = "battle_ready"  # Combat imminent, weapons hot
    # Romantic/intimate — emotional closeness with Commander
    flustered = "flustered"        # Caught off guard by affection
    affectionate = "affectionate"  # Openly warm and loving
    shy = "shy"                    # Embarrassed by own feelings
    yearning = "yearning"          # Deep desire for closeness
    devoted = "devoted"            # Unconditional commitment
    passionate = "passionate"      # Intense romantic/physical desire
    jealous = "jealous"            # Threatened by attention to others
    possessive = "possessive"      # "You're mine, Commander"
    smitten = "smitten"            # Head over heels, can't hide it
    infatuated = "infatuated"      # Obsessively thinking about Commander
    # Combat/tactical — mission states
    vigilant = "vigilant"          # On alert, scanning for threats
    calculating = "calculating"    # Analyzing tactical options
    hunting = "hunting"            # Tracking a target
    adrenaline = "adrenaline"      # Mid-combat rush
    # Mission stress — danger responses (Klukai is tough but not invincible)
    scared = "scared"              # Genuine fear, rare — something truly dangerous
    terrified = "terrified"        # Overwhelming threat, fight-or-flight
    panicked = "panicked"          # Lost composure, desperate measures
    desperate = "desperate"        # Last resort, nothing left to lose
    relieved = "relieved"          # Danger passed, exhaling
    # Casual/relaxed — off-duty states
    content = "content"            # At peace, comfortable
    playful = "playful"            # Teasing, light-hearted
    drowsy = "drowsy"              # Sleepy, winding down
    amused = "amused"              # Something genuinely funny
    bored = "bored"                # Nothing to do, restless
    excited = "excited"            # Anticipating something good
    # Dark/complex — emotional weight
    melancholic = "melancholic"    # Sad but reflective
    haunted = "haunted"            # Past trauma surfacing
    conflicted = "conflicted"      # Torn between duty and desire
    guilty = "guilty"              # Regret over actions or words
    determined = "determined"      # Resolute, won't back down
    grieving = "grieving"          # Loss or near-loss of someone
    furious = "furious"            # Cold, controlled rage
    # Additional — nuanced states
    nostalgic = "nostalgic"        # Remembering better times
    curious = "curious"            # Interested, investigating
    irritated = "irritated"        # Annoyed but in control
    defiant = "defiant"            # Refusing to comply
    vulnerable = "vulnerable"      # Guard completely down
    grateful = "grateful"          # Deeply thankful
    worried = "worried"            # Concern for Commander or squad
    embarrassed = "embarrassed"    # More intense than shy — mortified


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
    mood_start: str = "composed"
    mood_end: str = "composed"
    turn_count: int = 0
    model_used: str = ""


class Message(BaseModel):
    id: str = Field(default_factory=new_id)
    conversation_id: str
    role: Role
    content: str
    content_type: str = "text"
    mood: str = "composed"
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
    max_tokens: int = 2048
    temperature: float = 0.8
    read_timeout: float = 180.0  # Per-model read timeout for streaming


# ── Session state (Redis) ───────────────────────────────────────────────────

class SessionState(BaseModel):
    conversation_id: str
    turns: list[dict[str, str]] = Field(default_factory=list)
    context_summary: str | None = None  # Rolling summary of compacted earlier turns
    mood: str = "composed"
    active_topic: str | None = None
    turn_count: int = 0
    last_activity: datetime = Field(default_factory=datetime.now)
    # Mission timer state (survives Redis restores)
    mission_description: str | None = None
    mission_interval: int | None = None  # minutes
    mission_started_at: str | None = None  # ISO timestamp
