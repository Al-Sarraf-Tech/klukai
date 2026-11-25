"""Portrait frame URLs must still verify after the image cache window."""

from __future__ import annotations

from unittest.mock import patch

from app.helpers import asks_to_be_remembered
from app.portrait_assets import FRAME_CACHE_S, frame_url, verify_frame
from app.portrait_style import SIGN_BUCKET_S
from app.routes_portrait import FRAME_CACHE


def _sig_at(when: float) -> tuple[str, int]:
    with (
        patch("app.portrait_assets.time.time", return_value=when),
        patch("app.signed_urls.time.time", return_value=when),
    ):
        url = frame_url("claude", "speed_star", "blush", 1)
    sig = url.split("sig=", 1)[1]
    return sig, int(sig.split(".", 1)[0])


def test_frame_cache_header_matches_the_signature_window():
    assert FRAME_CACHE == f"private, max-age={FRAME_CACHE_S}"


def test_bucket_edge_signature_survives_a_cached_revalidation():
    """One second before the bucket rolls is the shortest signature.

    The image is fetched after the URL is signed, then cached for max-age.
    Revalidation used to 403 (expression overlay went blank) because that
    fetch starts the cache clock after exp was already fixed.
    """
    signed_at = float(SIGN_BUCKET_S * 5 + SIGN_BUCKET_S - 1)
    sig, exp = _sig_at(signed_at)
    fetched_at = signed_at + 2.0
    revalidate_at = fetched_at + FRAME_CACHE_S
    assert revalidate_at > exp  # nominal exp has passed

    with patch("app.signed_urls.time.time", return_value=revalidate_at):
        assert verify_frame("claude", "speed_star", "blush", sig)
        assert not verify_frame("claude", "speed_star", "smile", sig)
        assert not verify_frame("claude", "speed_star", "blush", f"{exp}.not-the-mac")

    with patch("app.signed_urls.time.time", return_value=exp + FRAME_CACHE_S + 1):
        assert not verify_frame("claude", "speed_star", "blush", sig)


def test_short_forgot_still_asks_to_be_remembered():
    assert asks_to_be_remembered("you forgot")
    assert asks_to_be_remembered("don't forget")
    assert not asks_to_be_remembered("thanks")
