"""Legacy art template helpers; rendering is disabled by owner policy."""
from __future__ import annotations

from unittest.mock import patch


from app.proactive import events as ev


class TestSpontaneousArt:
    def test_pieces_fallback_shape(self):
        pieces = ev._spontaneous_art_pieces()
        assert len(pieces) >= 3
        assert all({"scene", "annotation", "message"} <= set(p) for p in pieces)

    def test_pieces_uses_yaml_when_valid(self):
        good = [{"scene": "s", "annotation": "a", "message": "m"}]
        with patch.object(ev, "_raw_content", return_value=good):
            assert ev._spontaneous_art_pieces() == good
        # Malformed YAML (missing keys) falls back to the literal.
        with patch.object(ev, "_raw_content", return_value=[{"scene": "only"}]):
            assert ev._spontaneous_art_pieces() is ev._SPONTANEOUS_ART_PIECES_FALLBACK
