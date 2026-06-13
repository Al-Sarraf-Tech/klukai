"""Tests for app/voice_archive.py — voice letters (async JP voice notes).

All external services are mocked: the voice HTTP POST (httpx.AsyncClient.post),
the audio volume (AUDIO_DIR via tmp_path monkeypatch), and the DB pool.
NO real network and NO real disk under /audio.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Fakes ────────────────────────────────────────────────────────────────────

class _FakeVoiceResponse:
    """Stand-in for the httpx.Response from the voice /tts endpoint."""

    def __init__(self, status_code: int = 200, content: bytes = b"RIFFwav-bytes"):
        self.status_code = status_code
        self.content = content


class _RecordingConn:
    """Fake psycopg async connection that records execute() calls.

    rows: list of values returned by fetchone() (popped per execute).
    rowcount: value exposed on the execute result (for UPDATE assertions).
    """

    def __init__(self, fetchone_rows=None, rowcount=1):
        self.executed: list[tuple] = []
        self.committed = False
        self._fetchone_rows = list(fetchone_rows or [])
        self._rowcount = rowcount

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        result = MagicMock()
        result.rowcount = self._rowcount
        if self._fetchone_rows:
            result.fetchone = AsyncMock(return_value=self._fetchone_rows.pop(0))
        else:
            result.fetchone = AsyncMock(return_value=None)
        return result

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return self._conn


def _voice_post(status_code: int = 200, content: bytes = b"RIFFwav-bytes") -> AsyncMock:
    """An AsyncMock standing in for httpx.AsyncClient.post returning a voice resp."""
    return AsyncMock(return_value=_FakeVoiceResponse(status_code, content))


# ═══════════════════════════════════════════════════════════════════════════
# save_voice_note — happy path
# ═══════════════════════════════════════════════════════════════════════════


class TestSaveVoiceNoteHappy:
    @pytest.mark.asyncio
    async def test_happy_path_writes_wav_and_inserts_row(self, tmp_path, monkeypatch):
        from app import voice_archive

        monkeypatch.setattr(voice_archive, "AUDIO_DIR", tmp_path)
        conn = _RecordingConn()

        post = _voice_post(200, b"RIFF....WEBPwav")
        with patch("httpx.AsyncClient.post", post), \
             patch("app.voice_archive.get_pool", return_value=_Pool(conn)), \
             patch("app.helpers.voice_auth_headers", return_value={}):
            note_id = await voice_archive.save_voice_note(
                "Welcome back, Commander. I missed you.",
                user_id="alice",
                kind="reflection",
                mood="tender",
                affection_level=7,
                conversation_id="conv-1",
            )

        assert note_id is not None
        # WAV written to AUDIO_DIR/{id}.wav with the synthesized bytes.
        wav = tmp_path / f"{note_id}.wav"
        assert wav.exists()
        assert wav.read_bytes() == b"RIFF....WEBPwav"
        # Exactly one INSERT, committed, with the right user + metadata bound.
        assert conn.committed is True
        assert len(conn.executed) == 1
        sql, params = conn.executed[0]
        assert "INSERT INTO companion_voice_notes" in sql
        assert params[0] == note_id          # id
        assert params[1] == f"{note_id}.wav"  # audio_filename
        assert params[2] == "Welcome back, Commander. I missed you."  # text_prompt
        assert params[3] == "reflection"      # kind
        assert params[4] == "tender"          # mood
        assert params[5] == 7                 # affection_level
        assert params[6] == "conv-1"          # conversation_id
        assert params[7] == "alice"           # user_id

    @pytest.mark.asyncio
    async def test_posts_japanese_language_and_auth_headers(self, tmp_path, monkeypatch):
        """The synth POST mirrors /api/tts: {text, language} + voice auth headers,
        and defaults to Klukai's JP voice."""
        from app import voice_archive

        monkeypatch.setattr(voice_archive, "AUDIO_DIR", tmp_path)
        conn = _RecordingConn()
        post = _voice_post(200)

        with patch("httpx.AsyncClient.post", post), \
             patch("app.voice_archive.get_pool", return_value=_Pool(conn)), \
             patch("app.helpers.voice_auth_headers", return_value={"Authorization": "Bearer vtok"}):
            await voice_archive.save_voice_note("こんにちは指揮官", user_id="alice")

        # URL ends with /tts; body carries the JP language and the auth header.
        call = post.await_args
        assert call.args[0].endswith("/tts")
        assert call.kwargs["json"]["language"] == "ja"
        assert call.kwargs["json"]["text"] == "こんにちは指揮官"
        assert call.kwargs["headers"] == {"Authorization": "Bearer vtok"}

    @pytest.mark.asyncio
    async def test_truncates_long_text_for_synth_but_stores_full(self, tmp_path, monkeypatch):
        from app import voice_archive

        monkeypatch.setattr(voice_archive, "AUDIO_DIR", tmp_path)
        conn = _RecordingConn()
        post = _voice_post(200)
        long_text = "x" * 900

        with patch("httpx.AsyncClient.post", post), \
             patch("app.voice_archive.get_pool", return_value=_Pool(conn)), \
             patch("app.helpers.voice_auth_headers", return_value={}):
            await voice_archive.save_voice_note(long_text, user_id="alice")

        # Synth text capped at 500 chars …
        assert len(post.await_args.kwargs["json"]["text"]) == 500
        # … but the full text is persisted as text_prompt.
        assert conn.executed[0][1][2] == long_text


# ═══════════════════════════════════════════════════════════════════════════
# save_voice_note — FAIL-SOFT (every failure → None, no row)
# ═══════════════════════════════════════════════════════════════════════════


class TestSaveVoiceNoteFailSoft:
    @pytest.mark.asyncio
    async def test_voice_500_returns_none_and_no_row(self, tmp_path, monkeypatch):
        from app import voice_archive

        monkeypatch.setattr(voice_archive, "AUDIO_DIR", tmp_path)
        conn = _RecordingConn()
        post = _voice_post(500, b"TTS failed")

        with patch("httpx.AsyncClient.post", post), \
             patch("app.voice_archive.get_pool", return_value=_Pool(conn)) as gp, \
             patch("app.helpers.voice_auth_headers", return_value={}):
            note_id = await voice_archive.save_voice_note("hi", user_id="alice")

        assert note_id is None
        # No WAV written, no DB touched at all (pool never even opened).
        assert list(tmp_path.iterdir()) == []
        gp.assert_not_called()

    @pytest.mark.asyncio
    async def test_voice_exception_returns_none(self, tmp_path, monkeypatch):
        from app import voice_archive

        monkeypatch.setattr(voice_archive, "AUDIO_DIR", tmp_path)
        post = AsyncMock(side_effect=RuntimeError("connection refused"))

        with patch("httpx.AsyncClient.post", post), \
             patch("app.voice_archive.get_pool") as gp, \
             patch("app.helpers.voice_auth_headers", return_value={}):
            note_id = await voice_archive.save_voice_note("hi", user_id="alice")

        assert note_id is None
        assert list(tmp_path.iterdir()) == []
        gp.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_audio_returns_none(self, tmp_path, monkeypatch):
        from app import voice_archive

        monkeypatch.setattr(voice_archive, "AUDIO_DIR", tmp_path)
        post = _voice_post(200, b"")  # 200 but no bytes

        with patch("httpx.AsyncClient.post", post), \
             patch("app.voice_archive.get_pool") as gp, \
             patch("app.helpers.voice_auth_headers", return_value={}):
            note_id = await voice_archive.save_voice_note("hi", user_id="alice")

        assert note_id is None
        assert list(tmp_path.iterdir()) == []
        gp.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_text_short_circuits(self, tmp_path, monkeypatch):
        from app import voice_archive

        monkeypatch.setattr(voice_archive, "AUDIO_DIR", tmp_path)
        post = _voice_post(200)
        with patch("httpx.AsyncClient.post", post) as p:
            assert await voice_archive.save_voice_note("   ", user_id="alice") is None
        p.assert_not_called()  # never even hits the voice service

    @pytest.mark.asyncio
    async def test_disk_write_failure_returns_none(self, tmp_path, monkeypatch):
        from app import voice_archive

        monkeypatch.setattr(voice_archive, "AUDIO_DIR", tmp_path)
        post = _voice_post(200)

        with patch("httpx.AsyncClient.post", post), \
             patch("app.voice_archive.get_pool") as gp, \
             patch("app.helpers.voice_auth_headers", return_value={}), \
             patch("pathlib.Path.write_bytes", side_effect=OSError("disk full")):
            note_id = await voice_archive.save_voice_note("hi", user_id="alice")

        assert note_id is None
        gp.assert_not_called()  # DB never reached after a write failure

    @pytest.mark.asyncio
    async def test_db_insert_failure_returns_none_and_cleans_up_wav(self, tmp_path, monkeypatch):
        from app import voice_archive

        monkeypatch.setattr(voice_archive, "AUDIO_DIR", tmp_path)
        post = _voice_post(200, b"RIFFwav")

        broken_pool = MagicMock()
        broken_pool.connection.side_effect = RuntimeError("db down")

        with patch("httpx.AsyncClient.post", post), \
             patch("app.voice_archive.get_pool", return_value=broken_pool), \
             patch("app.helpers.voice_auth_headers", return_value={}):
            note_id = await voice_archive.save_voice_note("hi", user_id="alice")

        assert note_id is None
        # Orphaned WAV cleaned up so disk doesn't leak on DB outage.
        assert list(tmp_path.iterdir()) == []


# ═══════════════════════════════════════════════════════════════════════════
# get_voice_note — ownership + missing file/row
# ═══════════════════════════════════════════════════════════════════════════


class TestGetVoiceNote:
    @pytest.mark.asyncio
    async def test_owner_gets_bytes_and_filename(self, tmp_path, monkeypatch):
        from app import voice_archive

        monkeypatch.setattr(voice_archive, "AUDIO_DIR", tmp_path)
        (tmp_path / "n1.wav").write_bytes(b"RIFFaudio")
        conn = _RecordingConn(fetchone_rows=[("n1.wav",)])

        with patch("app.voice_archive.get_pool", return_value=_Pool(conn)):
            result = await voice_archive.get_voice_note("n1", "alice")

        assert result is not None
        audio, filename = result
        assert audio == b"RIFFaudio"
        assert filename == "n1.wav"
        # Query was scoped to the user.
        assert conn.executed[0][1] == ("n1", "alice")

    @pytest.mark.asyncio
    async def test_non_owner_or_missing_row_returns_none(self, tmp_path, monkeypatch):
        from app import voice_archive

        monkeypatch.setattr(voice_archive, "AUDIO_DIR", tmp_path)
        conn = _RecordingConn(fetchone_rows=[None])  # no row for this (id, user)

        with patch("app.voice_archive.get_pool", return_value=_Pool(conn)):
            result = await voice_archive.get_voice_note("n1", "mallory")

        assert result is None

    @pytest.mark.asyncio
    async def test_missing_file_on_disk_returns_none(self, tmp_path, monkeypatch):
        from app import voice_archive

        monkeypatch.setattr(voice_archive, "AUDIO_DIR", tmp_path)
        # Row exists but the file is gone from the volume.
        conn = _RecordingConn(fetchone_rows=[("gone.wav",)])

        with patch("app.voice_archive.get_pool", return_value=_Pool(conn)):
            result = await voice_archive.get_voice_note("n1", "alice")

        assert result is None

    @pytest.mark.asyncio
    async def test_db_error_returns_none(self, tmp_path, monkeypatch):
        from app import voice_archive

        monkeypatch.setattr(voice_archive, "AUDIO_DIR", tmp_path)
        with patch("app.voice_archive.get_pool", side_effect=RuntimeError("db down")):
            assert await voice_archive.get_voice_note("n1", "alice") is None


# ═══════════════════════════════════════════════════════════════════════════
# latest_voice_note
# ═══════════════════════════════════════════════════════════════════════════


class TestLatestVoiceNote:
    @pytest.mark.asyncio
    async def test_returns_metadata_dict(self):
        from app import voice_archive

        ts = datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc)
        conn = _RecordingConn(fetchone_rows=[("id-1", "reflection", "tender", 7, None, ts)])

        with patch("app.voice_archive.get_pool", return_value=_Pool(conn)):
            note = await voice_archive.latest_voice_note("alice")

        assert note == {
            "id": "id-1",
            "kind": "reflection",
            "mood": "tender",
            "affection_level": 7,
            "played": False,
            "played_at": None,
            "created_at": ts.isoformat(),
        }

    @pytest.mark.asyncio
    async def test_played_flag_true_when_played_at_set(self):
        from app import voice_archive

        ts = datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc)
        played = datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc)
        conn = _RecordingConn(fetchone_rows=[("id-1", "reflection", None, 0, played, ts)])

        with patch("app.voice_archive.get_pool", return_value=_Pool(conn)):
            note = await voice_archive.latest_voice_note("alice")

        assert note["played"] is True
        assert note["played_at"] == played.isoformat()

    @pytest.mark.asyncio
    async def test_none_when_no_notes(self):
        from app import voice_archive

        conn = _RecordingConn(fetchone_rows=[None])
        with patch("app.voice_archive.get_pool", return_value=_Pool(conn)):
            assert await voice_archive.latest_voice_note("alice") is None

    @pytest.mark.asyncio
    async def test_db_error_returns_none(self):
        from app import voice_archive
        with patch("app.voice_archive.get_pool", side_effect=RuntimeError("db down")):
            assert await voice_archive.latest_voice_note("alice") is None


# ═══════════════════════════════════════════════════════════════════════════
# mark_played
# ═══════════════════════════════════════════════════════════════════════════


class TestMarkPlayed:
    @pytest.mark.asyncio
    async def test_updates_returns_true(self):
        from app import voice_archive

        conn = _RecordingConn(rowcount=1)
        with patch("app.voice_archive.get_pool", return_value=_Pool(conn)):
            ok = await voice_archive.mark_played("id-1", "alice")

        assert ok is True
        assert conn.committed is True
        sql, params = conn.executed[0]
        assert "UPDATE companion_voice_notes SET played_at = NOW()" in sql
        assert "played_at IS NULL" in sql  # idempotent — only sets once
        assert params == ("id-1", "alice")

    @pytest.mark.asyncio
    async def test_no_rows_returns_false(self):
        from app import voice_archive

        conn = _RecordingConn(rowcount=0)  # already played, or not owner/missing
        with patch("app.voice_archive.get_pool", return_value=_Pool(conn)):
            assert await voice_archive.mark_played("id-1", "alice") is False

    @pytest.mark.asyncio
    async def test_db_error_returns_false(self):
        from app import voice_archive
        with patch("app.voice_archive.get_pool", side_effect=RuntimeError("db down")):
            assert await voice_archive.mark_played("id-1", "alice") is False
