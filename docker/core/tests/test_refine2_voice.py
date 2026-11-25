"""Japanese taps use background text, and a long line's tail is spoken.

No test calls Fish or a paid model. Translation is a fake background reply.
Synthesis is a fake WAV.
"""

from __future__ import annotations

import base64
import inspect
import struct
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app import voice_language
from app.extraction_llm import CLAUDE_EFFORT, EFFORT
from app.voice_client import join_wavs, speech_chunks


def _wav(pcm: bytes) -> bytes:
    fmt = struct.pack("<HHIIHH", 1, 1, 24000, 48000, 2, 16)
    fmt_chunk = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    data_chunk = b"data" + struct.pack("<I", len(pcm)) + pcm
    body = b"WAVE" + fmt_chunk + data_chunk
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.fixture
def no_translation_cache(monkeypatch):
    from app import caches

    async def _miss(_text: str) -> None:
        return None

    async def _store(_text: str, _japanese: str) -> None:
        return None

    monkeypatch.setattr(caches, "get_voice_translation", _miss)
    monkeypatch.setattr(caches, "put_voice_translation", _store)


def test_translation_source_is_not_interactive_chat():
    source = inspect.getsource(voice_language._ask_llm)
    assert "background_llm.text" in source
    assert "interactive=True" not in source
    assert "router.route" not in source
    assert "router.stream" not in source
    # The background route's effort, both providers. Chat xhigh is not this.
    assert CLAUDE_EFFORT == "low"
    assert EFFORT == "low"


@pytest.mark.asyncio
async def test_japanese_tap_uses_background_text(no_translation_cache):
    ask = AsyncMock(return_value="404小隊、出撃します。")
    with patch("app.background_llm.text", ask):
        spoken = await voice_language.translate_to_japanese("404, moving out.")
    assert spoken == "ヨンマルヨン小隊、出撃します。"
    ask.assert_awaited_once()
    args, kwargs = ask.await_args
    assert args == ("voice_translation", "404, moving out.")
    assert kwargs["system"] == voice_language.TRANSLATION_PROMPT
    assert kwargs["max_tokens"] == voice_language._TRANSLATION_MAX_TOKENS
    assert "interactive" not in kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "spoken"),
    [
        ("「（微笑む）ヨンマルヨンの任務です。」", "ヨンマルヨンの任務です。"),
        ("４０４です。", "ヨンマルヨンです。"),
        ("番号は14045です。", "番号は14045です。"),
        ("*nods* 了解しました、指揮官。", "了解しました、指揮官。"),
        ("<think>plan</think>```\nはい。\n```", "はい。"),
    ],
)
async def test_japanese_cleaning_and_404_reading(no_translation_cache, raw, spoken):
    with patch("app.background_llm.text", AsyncMock(return_value=raw)):
        assert await voice_language.translate_to_japanese("line") == spoken


@pytest.mark.asyncio
async def test_long_japanese_keeps_the_tail(no_translation_cache):
    sentence = "指揮官、報告は以上です。"
    raw = sentence * 60
    assert len(raw) > 500
    with patch("app.background_llm.text", AsyncMock(return_value=raw)):
        spoken = await voice_language.translate_to_japanese("The report is finished.")
    assert spoken == raw
    assert spoken.endswith(sentence)


def test_speech_chunks_return_the_tail_in_order():
    assert speech_chunks("x" * 900) == ["x" * 500, "x" * 400]
    assert "".join(speech_chunks("x" * 900)) == "x" * 900

    early = "Hi. " + "y" * 100
    hard = speech_chunks(early, 60)
    assert hard[0] == early[:60]
    assert "".join(hard) == early

    sentence = "Welcome back, Commander. "
    long = sentence * 40
    chunks = speech_chunks(long)
    assert len(chunks) > 1
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert chunks[0].endswith("Commander.")
    assert chunks[-1].startswith("Welcome back, Commander.")
    assert sum(chunk.count("Welcome back, Commander.") for chunk in chunks) == 40


def test_join_wavs_keeps_clip_audio_in_order():
    first, second = b"CLIP-ONE-PCM!!", b"CLIP-TWO-PCM!!"
    merged = join_wavs([_wav(first), _wav(second)])
    assert merged[:4] == b"RIFF" and merged[8:12] == b"WAVE"
    assert merged.find(first) < merged.find(second)
    assert join_wavs([b"not-a-wav", b"tail"]) == b"not-a-wavtail"


@pytest.mark.asyncio
async def test_job_speaks_later_chunks_in_order(monkeypatch):
    from app import voice_jobs

    sentence = "Welcome back, Commander. "
    text = sentence * 40
    calls: list[str] = []

    async def post(payload, timeout=None):
        calls.append(payload["text"])
        pcm = f"CLIP-{len(calls):02d}-PCM!!".encode()
        return httpx.Response(
            200,
            content=_wav(pcm),
            request=httpx.Request("POST", "https://api.fish.audio/v1/tts"),
        )

    monkeypatch.setattr(voice_jobs, "post_tts", post)
    encoded = await voice_jobs._synthesize({"text": text, "language": "en"})
    assert len(calls) > 1
    assert all(len(chunk) <= 500 for chunk in calls)
    assert calls[0].endswith("Commander.")
    assert sum(chunk.count("Welcome back, Commander.") for chunk in calls) == 40
    audio = base64.b64decode(encoded)
    assert audio.find(b"CLIP-01-PCM!!") < audio.find(b"CLIP-02-PCM!!")


class _Conn:
    def __init__(self):
        self.executed: list[tuple] = []

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return None

    async def commit(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return self._conn


@pytest.mark.asyncio
async def test_voice_letter_speaks_later_chunks_in_order(tmp_path, monkeypatch):
    from app import voice_archive, voice_client

    monkeypatch.setattr(voice_client, "voice_letters_enabled", lambda: True)
    monkeypatch.setattr(voice_archive, "AUDIO_DIR", tmp_path)
    calls: list[str] = []

    async def post(payload, timeout=None):
        calls.append(payload["text"])
        pcm = f"CLIP-{len(calls):02d}-PCM!!".encode()
        return httpx.Response(
            200,
            content=_wav(pcm),
            request=httpx.Request("POST", "https://api.fish.audio/v1/tts"),
        )

    text = "x" * 900
    conn = _Conn()
    with (
        patch("app.voice_client.post_tts", post),
        patch("app.voice_archive.get_pool", return_value=_Pool(conn)),
    ):
        note_id = await voice_archive.save_voice_note(text, user_id="alice")

    assert note_id is not None
    assert calls == ["x" * 500, "x" * 400]
    stored = (tmp_path / f"{note_id}.wav").read_bytes()
    assert stored.find(b"CLIP-01-PCM!!") < stored.find(b"CLIP-02-PCM!!")
    assert conn.executed[0][1][2] == text
