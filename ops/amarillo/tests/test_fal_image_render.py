"""Seedream 4.5 on fal, Klukai's sole picture provider: locked anime style,
approved references, caps reserved before every paid call, nothing leaked."""

import base64
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import sys
import urllib.error

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import agy_art_runtime as agy  # noqa: E402
import fal_image_render as fal  # noqa: E402

KEY = "fal-unit-key:never-printed"
REQUEST = {
    "request_id": "r1",
    "user_id": "claude",
    "prompt": "1girl, adult woman, rooftop garden at golden hour, trench coat",
    "width": 832,
    "height": 1216,
    "include_commander": False,
}


def png(color="green", size=(1664, 2432)):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeFal:
    """The queue API: submit, poll status, fetch the inline (sync_mode) result."""

    def __init__(self, result=None, submit_error=None, polls=1):
        self.calls, self.result, self.submit_error, self.polls = [], result, submit_error, polls

    def __call__(self, request, timeout):
        assert timeout <= 60
        url, method = request.full_url, request.get_method()
        body = json.loads(request.data) if request.data else None
        self.calls.append((method, url, request.get_header("Authorization"), body))
        if method == "POST":
            if self.submit_error:
                raise self.submit_error
            return Response(json.dumps({
                "request_id": "q1",
                "status_url": "https://queue.fal.run/x/requests/q1/status",
                "response_url": "https://queue.fal.run/x/requests/q1",
            }).encode())
        if url.endswith("/status"):
            self.polls -= 1
            return Response(json.dumps({"status": "COMPLETED" if self.polls <= 0 else "IN_PROGRESS"}).encode())
        result = self.result if self.result is not None else {
            "images": [{"url": "data:image/png;base64," + base64.b64encode(png()).decode()}],
            "has_nsfw_concepts": [False],
        }
        return Response(json.dumps(result).encode())


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("KLUKAI_FAL_LEDGER", str(tmp_path / "fal.sqlite3"))
    monkeypatch.setenv("KLUKAI_FAL_DAILY_LIMIT", "10")
    monkeypatch.setenv("KLUKAI_FAL_MONTHLY_LIMIT", "150")
    monkeypatch.setattr(fal, "fal_key", lambda: KEY)
    monkeypatch.setattr(fal, "read_image", lambda entry: png("blue", (64, 96)) if entry == "klukai" else png("red", (64, 64)))
    monkeypatch.setattr(fal.time, "sleep", lambda s: None)
    api = FakeFal()
    monkeypatch.setattr(fal, "_urlopen", api)
    return api, tmp_path


def attempts(tmp_path):
    with closing(sqlite3.connect(tmp_path / "fal.sqlite3")) as db:
        return db.execute("SELECT COUNT(*) FROM fal_attempts").fetchone()[0]


def test_one_render_sends_locked_style_reference_and_caps(setup):
    api, tmp_path = setup
    out = fal.render(REQUEST, {"klukai": "klukai", "commander": "commander"})
    assert out.startswith(b"\x89PNG")
    method, url, auth, body = api.calls[0]
    assert (method, url) == ("POST", "https://queue.fal.run/fal-ai/bytedance/seedream/v4.5/edit")
    assert auth == f"Key {KEY}"
    assert len(body["image_urls"]) == 1 and body["image_urls"][0].startswith("data:image/jpeg;base64,")
    assert body["sync_mode"] is True and body["enable_safety_checker"] is True
    assert body["num_images"] == 1
    assert body["image_size"] == {"width": 1648, "height": 2416}
    prompt = body["prompt"]
    assert prompt.startswith("2D anime illustration")
    for phrase in ("NOT photorealistic", "hand-painted anime background art", "Only Klukai in the image",
                   "green eyes", "rooftop garden at golden hour"):
        assert phrase in prompt
    assert attempts(tmp_path) == 1


def test_commander_reference_only_for_the_owner(setup):
    api, _ = setup
    fal.render(dict(REQUEST, user_id="jalsarraf", include_commander=True), {"klukai": "klukai", "commander": "commander"})
    body = api.calls[0][3]
    assert len(body["image_urls"]) == 2
    assert "Image 2 is the adult Commander" in body["prompt"] and "Exactly two people" in body["prompt"]
    api.calls.clear()
    with pytest.raises(agy.RuntimeError):
        fal.render(dict(REQUEST, include_commander=True), {"klukai": "klukai", "commander": "commander"})
    assert api.calls == []


@pytest.mark.parametrize("size,expected", [((832, 1216), (1648, 2416)), ((1024, 1024), (2000, 2000)),
                                           ((1216, 832), (2416, 1648))])
def test_size_scales_to_about_four_megapixels_in_sixteens(size, expected):
    width, height = fal.output_size(*size)
    assert (width, height) == expected
    assert width % 16 == 0 and height % 16 == 0 and 3_600_000 <= width * height <= 4_400_000


def test_exhausted_daily_cap_never_calls_fal(setup, monkeypatch):
    api, tmp_path = setup
    monkeypatch.setenv("KLUKAI_FAL_DAILY_LIMIT", "1")
    fal.render(REQUEST, {"klukai": "klukai"})
    api.calls.clear()
    with pytest.raises(agy.ProviderUnavailable, match="allowance"):
        fal.render(REQUEST, {"klukai": "klukai"})
    assert api.calls == [] and attempts(tmp_path) == 1


@pytest.mark.parametrize("daily,monthly", [("0", "150"), ("10", "0"), ("x", "150"), ("10", "")])
def test_missing_or_invalid_caps_fail_closed(setup, monkeypatch, daily, monthly):
    api, _ = setup
    monkeypatch.setenv("KLUKAI_FAL_DAILY_LIMIT", daily)
    monkeypatch.setenv("KLUKAI_FAL_MONTHLY_LIMIT", monthly)
    with pytest.raises(agy.ProviderUnavailable):
        fal.render(REQUEST, {"klukai": "klukai"})
    assert api.calls == []


@pytest.mark.parametrize("code,kind", [(401, agy.ProviderUnavailable), (403, agy.ProviderUnavailable),
                                       (429, agy.ProviderUnavailable), (503, agy.ProviderUnavailable),
                                       (422, agy.RuntimeError)])
def test_provider_errors_are_classified_and_never_echo_the_key(setup, monkeypatch, code, kind):
    api, tmp_path = setup
    error = urllib.error.HTTPError("https://queue.fal.run", code, "x", {}, io.BytesIO(KEY.encode()))
    monkeypatch.setattr(fal, "_urlopen", FakeFal(submit_error=error))
    with closing(error), pytest.raises(kind) as raised:
        fal.render(REQUEST, {"klukai": "klukai"})
    assert KEY not in str(raised.value) and str(code) in str(raised.value)
    assert attempts(tmp_path) == 1  # the paid attempt was reserved before dispatch


def test_network_failure_is_a_provider_outage(setup, monkeypatch):
    monkeypatch.setattr(fal, "_urlopen", FakeFal(submit_error=urllib.error.URLError("reset")))
    with pytest.raises(agy.ProviderUnavailable):
        fal.render(REQUEST, {"klukai": "klukai"})


@pytest.mark.parametrize("result", [{"images": [], "has_nsfw_concepts": []},
                                    {"images": [{"url": "data:image/png;base64,AAAA"}], "has_nsfw_concepts": [True]},
                                    {"images": [{"url": "https://v3.fal.media/x.png"}], "has_nsfw_concepts": [False]},
                                    {"detail": "nope"}])
def test_blocked_empty_or_remote_results_are_generation_failures(setup, monkeypatch, result):
    monkeypatch.setattr(fal, "_urlopen", FakeFal(result=result))
    with pytest.raises(agy.RuntimeError):
        fal.render(REQUEST, {"klukai": "klukai"})


def test_slow_job_times_out_as_an_outage(setup, monkeypatch):
    monkeypatch.setattr(fal, "_urlopen", FakeFal(polls=10_000))
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(fal.time, "monotonic", lambda: next(clock))
    with pytest.raises(agy.ProviderUnavailable, match="timed out"):
        fal.render(REQUEST, {"klukai": "klukai"})


def test_a_returned_reference_is_not_a_new_image(setup, monkeypatch):
    reference = png("blue", (64, 96))
    monkeypatch.setattr(fal, "_urlopen", FakeFal(result={
        "images": [{"url": "data:image/png;base64," + base64.b64encode(reference).decode()}],
        "has_nsfw_concepts": [False]}))
    with pytest.raises(agy.RuntimeError, match="reference"):
        fal.render(REQUEST, {"klukai": "klukai"})


def test_unavailable_references_fail_before_spending(setup, monkeypatch):
    api, tmp_path = setup
    monkeypatch.setattr(fal, "read_image", lambda entry: (_ for _ in ()).throw(ValueError("digest")))
    with pytest.raises(agy.RuntimeError):
        fal.render(REQUEST, {"klukai": "klukai"})
    assert api.calls == []


def test_key_comes_from_the_encrypted_store(monkeypatch):
    seen = []

    class Store:
        @staticmethod
        def decrypt(name):
            seen.append(name)
            return b"FAL_KEY=abc:def\nOTHER=x\n"

    monkeypatch.setattr(fal, "_secret_store", lambda: Store)
    assert fal.fal_key() == "abc:def" and seen == ["compose-env"]
    monkeypatch.setattr(fal, "_secret_store", lambda: type("S", (), {"decrypt": staticmethod(lambda n: b"OTHER=x\n")}))
    with pytest.raises(agy.ProviderUnavailable):
        fal.fal_key()



@pytest.mark.parametrize("include_commander", [False, True, None])
def test_unsupported_nudity_request_is_refused_before_any_private_or_paid_work(setup, monkeypatch, include_commander):
    from unittest.mock import Mock

    api, tmp_path = setup
    read = Mock(side_effect=AssertionError("references must not be read"))
    key = Mock(side_effect=AssertionError("credential must not be read"))
    reserve = Mock(side_effect=AssertionError("attempt must not be spent"))
    monkeypatch.setattr(fal, "read_image", read)
    monkeypatch.setattr(fal, "fal_key", key)
    monkeypatch.setattr(fal, "reserve_attempt", reserve)
    request = dict(REQUEST, nudity=True, include_commander=include_commander)
    with pytest.raises(agy.RuntimeError, match="unsupported"):
        fal.render(request, {"klukai": "klukai", "commander": "commander"})
    read.assert_not_called()
    key.assert_not_called()
    reserve.assert_not_called()
    assert api.calls == []
    assert not (tmp_path / "fal.sqlite3").exists()


@pytest.mark.parametrize("nudity", [False, "yes", 1, None])
def test_anything_but_the_literal_grant_keeps_the_filter_on(setup, nudity):
    api, _ = setup
    request = dict(REQUEST) if nudity is None else dict(REQUEST, nudity=nudity)
    fal.render(request, {"klukai": "klukai"})
    assert api.calls[0][3]["enable_safety_checker"] is True


def test_nudity_with_the_commander_reference_is_refused_before_spending(setup):
    api, tmp_path = setup
    with pytest.raises(agy.RuntimeError):
        fal.render(dict(REQUEST, user_id="jalsarraf", include_commander=True, nudity=True),
                   {"klukai": "klukai", "commander": "commander"})
    assert api.calls == []
