"""Validated voice service request contracts."""

from pydantic import BaseModel, Field


class TTSRequest(BaseModel):
    text: str
    language: str = "en"


# ~6 MB decoded is several minutes of Opus: far past any push-to-talk clip,
# and small enough that one request cannot pin the host decoding it.
MAX_STT_AUDIO_B64 = 8_000_000


class STTRequest(BaseModel):
    # A larger body is rejected at validation instead of being base64-decoded
    # into memory under the access lock; an empty one never loads Whisper.
    audio: str = Field(min_length=1, max_length=MAX_STT_AUDIO_B64)
