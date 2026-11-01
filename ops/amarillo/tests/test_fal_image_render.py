"""Seedream 4.5 on fal, Klukai's sole picture provider: locked anime style,
approved references, caps reserved before every paid call, nothing leaked."""

import base64
from contextlib import closing
import io
import http.client
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


def test_commander_is_described_in_text_without_reading_or_sending_his_image(setup, monkeypatch):
    api, _ = setup
    read = []
    original_read = fal.read_image
    monkeypatch.setattr(
        fal, "read_image", lambda entry: (read.append(entry), original_read(entry))[1]
    )
    fal.render(dict(REQUEST, user_id="jalsarraf", include_commander=True), {"klukai": "klukai", "commander": "commander"})
    body = api.calls[0][3]
    assert len(body["image_urls"]) == 1
    assert read == ["klukai"]
    assert "The adult human Commander" in body["prompt"]
    for detail in (
        "short dark crew-cut hair",
        "brown eyes",
        "light-olive skin",
        "mature masculine facial features",
        "strong build",
    ):
        assert detail in body["prompt"]
    assert "Exactly two people" in body["prompt"]
    assert "from the photo" not in body["prompt"].lower()
    assert "reference image of the Commander" not in body["prompt"]
    assert "Image 2 is the adult Commander" not in body["prompt"]
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
                                       (429, agy.ProviderUnavailable), (503, fal.PendingGeneration),
                                       (422, agy.RuntimeError)])
def test_provider_errors_are_classified_and_never_echo_the_key(setup, monkeypatch, code, kind):
    api, tmp_path = setup
    error = urllib.error.HTTPError("https://queue.fal.run", code, "x", {}, io.BytesIO(KEY.encode()))
    monkeypatch.setattr(fal, "_urlopen", FakeFal(submit_error=error))
    with closing(error), pytest.raises(kind) as raised:
        fal.render(REQUEST, {"klukai": "klukai"})
    assert KEY not in str(raised.value) and str(code) in str(raised.value)
    assert attempts(tmp_path) == 1  # the paid attempt was reserved before dispatch


@pytest.mark.parametrize("code,body,kind,message", [
    # A 422 is either a content refusal or a malformed request (a bug on this
    # side); the journal has to say which, never what was asked.
    (422, {"detail": [{"loc": ["body", "prompt"], "msg": "private scene words",
                       "type": "content_policy_violation", "input": "private scene words"}]},
     agy.ContentRefused, "fal refused the content (HTTP 422: content_policy_violation)"),
    (422, {"detail": [{"loc": ["body", "image_size"], "msg": "x", "type": "greater_than_equal"},
                      {"loc": ["body", "image_size"], "msg": "y", "type": "greater_than_equal"}]},
     agy.RuntimeError, "fal rejected the request (HTTP 422: greater_than_equal)"),
    (503, {"detail": "private scene words", "error_type": "runner_scheduling_failure"},
     fal.PendingGeneration, "fal submission outcome unknown (HTTP 503: runner_scheduling_failure)"),
    # Undocumented types are left out rather than echoed.
    (422, {"detail": [{"msg": "x", "type": "private scene words"}]},
     agy.RuntimeError, "fal rejected the request (HTTP 422)"),
    (422, {"detail": "private scene words"}, agy.RuntimeError, "fal rejected the request (HTTP 422)"),
])
def test_errors_name_only_documented_fal_error_types(setup, monkeypatch, code, body, kind, message):
    error = urllib.error.HTTPError("https://queue.fal.run", code, "x", {},
                                   io.BytesIO(json.dumps(body).encode()))
    monkeypatch.setattr(fal, "_urlopen", FakeFal(submit_error=error))
    with closing(error), pytest.raises(kind) as raised:
        fal.render(REQUEST, {"klukai": "klukai"})
    assert str(raised.value) == message


def test_network_failure_leaves_the_submission_uncertain(setup, monkeypatch):
    monkeypatch.setattr(fal, "_urlopen", FakeFal(submit_error=urllib.error.URLError("reset")))
    with pytest.raises(fal.PendingGeneration):
        fal.render(REQUEST, {"klukai": "klukai"})


@pytest.mark.parametrize("result", [{"images": [], "has_nsfw_concepts": []},
                                    {"images": [{"url": "data:image/png;base64,AAAA"}], "has_nsfw_concepts": [True]},
                                    {"images": [{"url": "https://v3.fal.media/x.png"}], "has_nsfw_concepts": [False]},
                                    {"detail": "nope"}])
def test_blocked_empty_or_remote_results_are_generation_failures(setup, monkeypatch, result):
    monkeypatch.setattr(fal, "_urlopen", FakeFal(result=result))
    with pytest.raises(agy.RuntimeError):
        fal.render(REQUEST, {"klukai": "klukai"})


def test_slow_job_times_out_without_authorizing_another_generation(setup, monkeypatch):
    monkeypatch.setattr(fal, "_urlopen", FakeFal(polls=10_000))
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(fal.time, "monotonic", lambda: next(clock))
    with pytest.raises(fal.PendingGeneration, match="timed out"):
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


@pytest.mark.parametrize("stage", ["status", "result"])
def test_transient_observation_recovers_without_another_paid_submission(setup, monkeypatch, stage):
    api, tmp_path = setup
    failed = False

    def flaky(request, timeout):
        nonlocal failed
        target = request.get_method() == "GET" and request.full_url.endswith("/status") == (stage == "status")
        if target and not failed:
            failed = True
            raise urllib.error.URLError("temporary disconnect")
        return api(request, timeout)

    monkeypatch.setattr(fal, "_urlopen", flaky)
    assert fal.render(REQUEST, {"klukai": "klukai"}).startswith(b"\x89PNG")
    assert failed and sum(call[0] == "POST" for call in api.calls) == 1
    assert attempts(tmp_path) == 1


@pytest.mark.parametrize("url", ["http://queue.fal.run/x", "https://other.test/x",
                                     "https://queue.fal.run@other.test/x",
                                     "https://queue.fal.run:444/x", "https://queue.fal.run/x#fragment"])
def test_queue_endpoints_are_validated_before_sending_a_key(setup, monkeypatch, url):
    calls = []

    def api(request, timeout):
        calls.append(request.full_url)
        assert len(calls) == 1, "untrusted endpoint must not be requested"
        return Response(json.dumps({"request_id": "q1", "status_url": url,
                                   "response_url": "https://queue.fal.run/x/requests/q1"}).encode())

    monkeypatch.setattr(fal, "_urlopen", api)
    with pytest.raises(agy.RuntimeError, match="endpoint"):
        fal.render(REQUEST, {"klukai": "klukai"})
    assert len(calls) == 1


@pytest.mark.parametrize("doc", [None, [], "invalid", {"status": "UNKNOWN"}])
def test_malformed_observation_fails_with_a_sanitized_error(setup, monkeypatch, doc):
    api, _ = setup
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(fal.time, "monotonic", lambda: next(clock))

    def malformed(request, timeout):
        if request.get_method() == "GET":
            return Response(json.dumps(doc).encode())
        return api(request, timeout)

    monkeypatch.setattr(fal, "_urlopen", malformed)
    with pytest.raises(agy.RuntimeError):
        fal.render(REQUEST, {"klukai": "klukai"})


def test_policy_refusal_takes_precedence_over_http_service_category(setup, monkeypatch):
    error = urllib.error.HTTPError("https://queue.fal.run", 403, "private", {},
                                  io.BytesIO(b'{"error_type":"content_policy_violation"}'))
    monkeypatch.setattr(fal, "_urlopen", FakeFal(submit_error=error))
    with closing(error), pytest.raises(agy.ContentRefused):
        fal.render(REQUEST, {"klukai": "klukai"})


def test_http_timeout_is_limited_by_remaining_render_budget(setup, monkeypatch):
    api, _ = setup
    current = [0.0]
    timeouts = []

    def delayed(request, timeout):
        timeouts.append(timeout)
        if request.get_method() == "POST":
            current[0] = fal.TIMEOUT_SECONDS - 3
        return api(request, timeout)

    monkeypatch.setattr(fal.time, "monotonic", lambda: current[0])
    monkeypatch.setattr(fal, "_urlopen", delayed)
    fal.render(REQUEST, {"klukai": "klukai"})
    assert all(0 < timeout <= 3 for timeout in timeouts[1:])


def test_submit_disconnect_is_not_safe_for_provider_failover(setup, monkeypatch):
    calls = []

    def disconnected(request, timeout):
        calls.append(request.get_method())
        raise urllib.error.URLError("connection lost after sending")

    monkeypatch.setattr(fal, "_urlopen", disconnected)
    with pytest.raises(agy.RuntimeError) as raised:
        fal.render(REQUEST, {"klukai": "klukai"})
    assert not isinstance(raised.value, agy.ProviderUnavailable)
    assert calls == ["POST"]


def test_authenticated_transport_does_not_follow_redirects():
    request = urllib.request.Request("https://queue.fal.run/x", headers={"Authorization": f"Key {KEY}"})
    with pytest.raises(urllib.error.HTTPError):
        fal.NoRedirect().redirect_request(request, None, 302, "redirect", {}, "https://other.test/")


@pytest.mark.parametrize("code", [408, 429, 500, 503])
def test_transient_status_http_error_retries_observation_only(setup, monkeypatch, code):
    api, tmp_path = setup
    failed = False

    def flaky(request, timeout):
        nonlocal failed
        if request.get_method() == "GET" and not failed:
            failed = True
            raise urllib.error.HTTPError(request.full_url, code, "private scene", {}, io.BytesIO(b"{}"))
        return api(request, timeout)

    monkeypatch.setattr(fal, "_urlopen", flaky)
    assert fal.render(REQUEST, {"klukai": "klukai"}).startswith(b"\x89PNG")
    assert attempts(tmp_path) == 1 and sum(c[0] == "POST" for c in api.calls) == 1


def test_persistent_observation_outage_is_bounded_and_not_failover_safe(setup, monkeypatch):
    api, _ = setup
    clock = iter(range(0, 10_000, 10))
    monkeypatch.setattr(fal.time, "monotonic", lambda: next(clock))
    gets = []

    def unavailable(request, timeout):
        if request.get_method() == "GET":
            gets.append(request.full_url)
            raise urllib.error.URLError("private details")
        return api(request, timeout)

    monkeypatch.setattr(fal, "_urlopen", unavailable)
    with pytest.raises(fal.PendingGeneration):
        fal.render(REQUEST, {"klukai": "klukai"})
    assert 1 < len(gets) < 20
    assert len(set(gets)) == 1 and sum(c[0] == "POST" for c in api.calls) == 1


@pytest.mark.parametrize("flags", [True, "false", ["false"], [None], {}])
def test_malformed_safety_metadata_is_never_treated_as_approval(setup, monkeypatch, flags):
    result = {"images": [{"url": "data:image/png;base64," + base64.b64encode(png()).decode()}],
              "has_nsfw_concepts": flags}
    monkeypatch.setattr(fal, "_urlopen", FakeFal(result=result))
    with pytest.raises(agy.RuntimeError, match="safety metadata"):
        fal.render(REQUEST, {"klukai": "klukai"})


def test_submitted_queue_handle_survives_a_polling_interruption(setup, monkeypatch):
    api, tmp_path = setup

    def interrupted(request, timeout):
        if request.get_method() == "GET":
            raise KeyboardInterrupt
        return api(request, timeout)

    monkeypatch.setattr(fal, "_urlopen", interrupted)
    with pytest.raises(KeyboardInterrupt):
        fal.render(REQUEST, {"klukai": "klukai"})
    with closing(sqlite3.connect(tmp_path / "fal.sqlite3")) as db:
        rows = db.execute("SELECT request_id, provider_request_id, status_url, response_url FROM fal_jobs").fetchall()
    assert rows == [("r1", "q1", "https://queue.fal.run/x/requests/q1/status",
                     "https://queue.fal.run/x/requests/q1")]


def test_job_endpoints_must_belong_to_returned_request(setup, monkeypatch):
    calls = []

    def mismatch(request, timeout):
        calls.append(request.get_method())
        assert len(calls) == 1
        return Response(json.dumps({"request_id": "q1", "status_url": "https://queue.fal.run/x/requests/other/status",
                                   "response_url": "https://queue.fal.run/x/requests/q1"}).encode())

    monkeypatch.setattr(fal, "_urlopen", mismatch)
    with pytest.raises(agy.RuntimeError, match="job"):
        fal.render(REQUEST, {"klukai": "klukai"})


@pytest.mark.parametrize("code", [401, 402, 403])
def test_observation_auth_failure_cannot_authorize_a_second_paid_generation(setup, monkeypatch, code):
    api, _ = setup

    def unavailable(request, timeout):
        if request.get_method() == "GET":
            raise urllib.error.HTTPError(request.full_url, code, "private", {}, io.BytesIO(b"{}"))
        return api(request, timeout)

    monkeypatch.setattr(fal, "_urlopen", unavailable)
    with pytest.raises(fal.PendingGeneration):
        fal.render(REQUEST, {"klukai": "klukai"})
    assert sum(c[0] == "POST" for c in api.calls) == 1


@pytest.mark.parametrize("stage", ["submit", "result"])
def test_response_finishing_after_deadline_is_not_accepted(setup, monkeypatch, stage):
    api, _ = setup
    current = [0.0]
    monkeypatch.setattr(fal.time, "monotonic", lambda: current[0])

    class LateResponse(Response):
        def read(self, *args):
            current[0] = fal.TIMEOUT_SECONDS + 1
            return super().read(*args)

    def late(request, timeout):
        response = api(request, timeout)
        if (request.get_method() == "POST" and stage == "submit"
                or request.get_method() == "GET" and not request.full_url.endswith("/status") and stage == "result"):
            return LateResponse(response.getvalue())
        return response

    monkeypatch.setattr(fal, "_urlopen", late)
    with pytest.raises(fal.PendingGeneration, match="timed out"):
        fal.render(REQUEST, {"klukai": "klukai"})


def test_incomplete_response_retries_observation_without_resubmission(setup, monkeypatch):
    api, _ = setup
    gets = [0]

    def interrupted(request, timeout):
        if request.get_method() == "GET":
            gets[0] += 1
            if gets[0] == 1:
                raise http.client.IncompleteRead(b"private partial data")
        return api(request, timeout)

    monkeypatch.setattr(fal, "_urlopen", interrupted)
    assert fal.render(REQUEST, {"klukai": "klukai"}).startswith(b"\x89PNG")
    assert sum(c[0] == "POST" for c in api.calls) == 1


def test_incomplete_submit_response_is_uncertain_and_sanitized(setup, monkeypatch):
    def interrupted(*args, **kwargs):
        raise http.client.IncompleteRead(b"private partial data")

    monkeypatch.setattr(fal, "_urlopen", interrupted)
    with pytest.raises(fal.PendingGeneration) as raised:
        fal.render(REQUEST, {"klukai": "klukai"})
    assert "private" not in str(raised.value)


def interrupted_job(setup, monkeypatch):
    api, tmp_path = setup

    def stop_after_submit(request, timeout):
        if request.get_method() == "GET":
            raise KeyboardInterrupt
        return api(request, timeout)

    monkeypatch.setattr(fal, "_urlopen", stop_after_submit)
    with pytest.raises(KeyboardInterrupt):
        fal.render(REQUEST, {"klukai": "klukai"})
    api.calls.clear()
    monkeypatch.setattr(fal, "_urlopen", api)
    return api, tmp_path


def test_recovery_fetches_existing_image_with_exhausted_caps_and_no_new_spend(setup, monkeypatch):
    api, tmp_path = interrupted_job(setup, monkeypatch)
    monkeypatch.setenv("KLUKAI_FAL_DAILY_LIMIT", "0")
    monkeypatch.setenv("KLUKAI_FAL_MONTHLY_LIMIT", "0")
    monkeypatch.setattr(fal, "reserve_attempt", lambda: pytest.fail("recovery cannot spend"))
    assert fal.recover(REQUEST, {"klukai": "klukai"}).startswith(b"\x89PNG")
    assert [call[0] for call in api.calls] == ["GET", "GET"]
    assert attempts(tmp_path) == 1
    assert fal.render.recover is fal.recover


def test_recovery_observes_in_progress_without_waiting_or_resubmitting(setup, monkeypatch):
    api, tmp_path = interrupted_job(setup, monkeypatch)
    api.polls = 2
    assert fal.recover(REQUEST, {"klukai": "klukai"}) is None
    assert len(api.calls) == 1 and api.calls[0][0] == "GET"
    assert api.calls[0][1].endswith("/requests/q1/status")
    assert attempts(tmp_path) == 1


@pytest.mark.parametrize("change", [{"prompt": "a different scene"}, {"user_id": "other-user"},
                                    {"request_id": "other-request"}, {"width": 1024}])
def test_recovery_requires_exact_stored_provider_request(setup, monkeypatch, change):
    api, _ = interrupted_job(setup, monkeypatch)
    monkeypatch.setattr(fal, "fal_key", lambda: pytest.fail("unmatched recovery cannot read key"))
    monkeypatch.setattr(fal, "read_image", lambda *_: pytest.fail("unmatched recovery cannot read references"))
    assert fal.recover(dict(REQUEST, **change), {"klukai": "klukai"}) is None
    assert api.calls == []


@pytest.mark.parametrize("legacy", [False, True])
def test_recovery_without_a_record_has_no_private_or_network_effects(setup, monkeypatch, legacy):
    api, tmp_path = setup
    if legacy:
        fal.reserve_attempt()  # old ledger without a fal_jobs table
    monkeypatch.setattr(fal, "fal_key", lambda: pytest.fail("missing handle cannot read key"))
    monkeypatch.setattr(fal, "read_image", lambda *_: pytest.fail("missing handle cannot read references"))
    assert fal.recover(REQUEST, {"klukai": "klukai"}) is None
    assert api.calls == []
    assert (tmp_path / "fal.sqlite3").exists() == legacy


def test_recovery_revalidates_persisted_endpoints_before_private_reads(setup, monkeypatch):
    api, tmp_path = interrupted_job(setup, monkeypatch)
    with closing(sqlite3.connect(tmp_path / "fal.sqlite3")) as db:
        db.execute("UPDATE fal_jobs SET status_url = 'https://untrusted.test/requests/q1/status'")
        db.commit()
    monkeypatch.setattr(fal, "fal_key", lambda: pytest.fail("invalid endpoint cannot read key"))
    with pytest.raises(agy.RuntimeError, match="endpoint"):
        fal.recover(REQUEST, {"klukai": "klukai"})
    assert api.calls == []


def test_recovery_outage_remains_pending_with_no_provider_failover(setup, monkeypatch):
    api, _ = interrupted_job(setup, monkeypatch)
    clock = iter(range(0, 10_000, 4))
    monkeypatch.setattr(fal.time, "monotonic", lambda: next(clock))

    def unavailable(request, timeout):
        assert request.get_method() == "GET"
        assert 0 < timeout <= 10
        raise urllib.error.URLError("private detail")

    monkeypatch.setattr(fal, "_urlopen", unavailable)
    with pytest.raises(agy.GenerationPending):
        fal.recover(REQUEST, {"klukai": "klukai"}, timeout=10)


def test_recovery_never_returns_a_filtered_result(setup, monkeypatch):
    api, _ = interrupted_job(setup, monkeypatch)
    api.result = {"images": [{"url": "data:image/png;base64," + base64.b64encode(png()).decode()}],
                  "has_nsfw_concepts": [True]}
    with pytest.raises(agy.RuntimeError):
        fal.recover(REQUEST, {"klukai": "klukai"})
    assert all(call[0] == "GET" for call in api.calls)


def test_recovery_does_not_return_the_approved_reference(setup, monkeypatch):
    api, _ = interrupted_job(setup, monkeypatch)
    api.result = {"images": [{"url": "data:image/png;base64," + base64.b64encode(png("blue", (64, 96))).decode()}],
                  "has_nsfw_concepts": [False]}
    with pytest.raises(agy.RuntimeError, match="reference"):
        fal.recover(REQUEST, {"klukai": "klukai"})


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_recovery_invalid_deadline_fails_before_private_work(setup, monkeypatch, timeout):
    api, _ = interrupted_job(setup, monkeypatch)
    monkeypatch.setattr(fal, "fal_key", lambda: pytest.fail("invalid deadline cannot read key"))
    with pytest.raises(agy.RuntimeError):
        fal.recover(REQUEST, {"klukai": "klukai"}, timeout=timeout)
    assert api.calls == []


@pytest.mark.parametrize("unavailable", ["reference", "key"])
def test_recovery_private_preflight_outage_preserves_existing_job(setup, monkeypatch, unavailable):
    api, tmp_path = interrupted_job(setup, monkeypatch)

    def fail(*args):
        raise agy.ProviderUnavailable("private details")

    monkeypatch.setattr(fal, "read_image" if unavailable == "reference" else "fal_key", fail)
    with pytest.raises(agy.GenerationPending) as raised:
        fal.recover(REQUEST, {"klukai": "klukai"})
    assert "private details" not in str(raised.value)
    assert api.calls == [] and attempts(tmp_path) == 1


def test_the_shared_brief_never_asks_for_sky():
    """Every renderer reads this. It contradicted indoor scenes and the checker failed them."""
    assert "sky" not in fal.BACKGROUND
    assert "buildings" not in fal.BACKGROUND
