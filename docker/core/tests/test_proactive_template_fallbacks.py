"""A malformed proactive_content entry in personality.yaml must fall back to the
in-code literal — a config typo can never silently empty one of her pools."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.proactive.templates import _content_list, _content_str_map, _int_keyed

_FALLBACK = {0: ["fallback line"]}


class TestIntKeyed:
    def test_string_keys_are_coerced(self):
        assert _int_keyed({"3": ["a"]}, _FALLBACK) == {3: ["a"]}

    @pytest.mark.parametrize("raw", [
        {},
        ["not", "a", "dict"],
        {"three": ["a"]},
        {1: "not a list"},
        {1: ["ok", 2]},
    ])
    def test_malformed_falls_back(self, raw):
        assert _int_keyed(raw, _FALLBACK) is _FALLBACK


def _with_yaml(value):
    return patch("app.proactive.templates._raw_content", return_value=value)


class TestContentList:
    def test_valid_list_loaded(self):
        with _with_yaml(["a", "b"]):
            assert _content_list("k", ["fb"]) == ["a", "b"]

    @pytest.mark.parametrize("raw", [None, [], ["a", 1], {"a": "b"}])
    def test_malformed_falls_back(self, raw):
        fb = ["fb"]
        with _with_yaml(raw):
            assert _content_list("k", fb) is fb


class TestContentStrMap:
    def test_valid_map_loaded_with_string_keys(self):
        with _with_yaml({1: ["a"], "victory": ["b"]}):
            assert _content_str_map("k", {}) == {"1": ["a"], "victory": ["b"]}

    @pytest.mark.parametrize("raw", [None, {}, ["a"], {"victory": "not a list"}, {"victory": ["a", 2]}])
    def test_malformed_falls_back(self, raw):
        fb = {"victory": ["fb"]}
        with _with_yaml(raw):
            assert _content_str_map("k", fb) is fb


class TestDreamPromptsFallback:
    def test_partial_yaml_keeps_every_dream_type(self):
        from app.proactive.events import _DREAM_PROMPTS_FALLBACK, _dream_prompts

        with patch("app.proactive.events._raw_content", return_value={"tender": "only one"}):
            assert _dream_prompts() is _DREAM_PROMPTS_FALLBACK


class TestTimeOfDay:
    @pytest.mark.parametrize("hour, bucket", [
        (5, "morning"), (11, "morning"), (12, "afternoon"), (16, "afternoon"),
        (17, "evening"), (20, "evening"), (21, "night"), (2, "night"),
    ])
    def test_buckets(self, hour, bucket):
        from app.proactive.events import _time_of_day

        assert _time_of_day(hour) == bucket
