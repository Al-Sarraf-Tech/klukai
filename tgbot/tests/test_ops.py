"""Tests for ops command handlers."""

from __future__ import annotations

import unittest

from tgbot.ops import truncate, format_mono, validate_service


class TestTruncate(unittest.TestCase):
    def test_short_text_unchanged(self):
        assert truncate("hello", 100) == "hello"

    def test_long_text_truncated(self):
        text = "x" * 5000
        result = truncate(text, 4000)
        assert len(result) <= 4000
        assert "truncated" in result

    def test_exact_limit_unchanged(self):
        text = "x" * 4000
        assert truncate(text, 4000) == text


class TestFormatMono(unittest.TestCase):
    def test_wraps_in_code_block(self):
        assert format_mono("hello") == "```\nhello\n```"

    def test_empty_input(self):
        assert format_mono("") == "```\n\n```"


class TestValidateService(unittest.TestCase):
    def test_valid_service(self):
        assert validate_service("core") == "core"
        assert validate_service("voice") == "voice"

    def test_invalid_service_returns_default(self):
        assert validate_service("rm -rf /") == "core"
        assert validate_service("'; DROP TABLE") == "core"


if __name__ == "__main__":
    unittest.main()
