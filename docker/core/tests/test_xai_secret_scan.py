"""The tracked-tree scan rejects an xAI key and accepts this checkout."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.ci.xai_secret_scan import scan_text, tracked_hits  # noqa: E402


def test_a_key_shaped_token_is_rejected_and_a_short_one_is_not():
    assert scan_text("prefix " + "xai-" + ("a" * 20) + " suffix")
    assert not scan_text("xai-" + ("a" * 19))
    assert not scan_text("unit-test-key")


def test_this_checkout_has_no_tracked_xai_key():
    assert tracked_hits(ROOT) == []
