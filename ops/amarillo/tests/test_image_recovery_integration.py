"""Real worker/renderer/ledger integration with only the remote transport faked."""

import base64
from contextlib import closing
import hashlib
import http.client
import io
import json
from pathlib import Path
import sqlite3
import sys
import urllib.error

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import agy_image_worker as worker  # noqa: E402
import fal_image_render as fal  # noqa: E402


def png(color, size):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    state = tmp_path / "worker"
    state.mkdir(mode=0o700)
    reference = tmp_path / "reference.png"
    raw = png("blue", (64, 96))
    reference.write_bytes(raw)
    references = {"klukai": {"path": str(reference), "sha256": hashlib.sha256(raw).hexdigest()}}
    value = worker.validate_request({
        "request_id": "763e0391-d3e5-4708-b7e8-3a21b3ed3939", "user_id": "alice",
        "prompt": "adult woman wearing a coat in a rooftop garden", "width": 832, "height": 1216,
        "include_commander": False,
    })
    ledger = tmp_path / "fal.sqlite3"
    monkeypatch.setenv("KLUKAI_FAL_LEDGER", str(ledger))
    monkeypatch.setenv("KLUKAI_FAL_DAILY_LIMIT", "1")
    monkeypatch.setenv("KLUKAI_FAL_MONTHLY_LIMIT", "1")
    monkeypatch.setattr(fal, "fal_key", lambda: "unit-key")
    output = png("green", (1664, 2432))
    calls = []

    def api(request, timeout):
        method, url = request.get_method(), request.full_url
        calls.append((method, url))
        if method == "POST":
            result = {"request_id": "queued1", "status_url": "https://queue.fal.run/x/requests/queued1/status",
                      "response_url": "https://queue.fal.run/x/requests/queued1"}
        elif url.endswith("/status"):
            result = {"status": "COMPLETED"}
        else:
            result = {"images": [{"url": "data:image/png;base64," + base64.b64encode(output).decode()}],
                      "has_nsfw_concepts": [False]}
        return io.BytesIO(json.dumps(result).encode())

    monkeypatch.setattr(fal, "_urlopen", api)

    def new_worker(checker):
        return worker.Worker(state, b"test-token", references, {"alice"},
                             providers=[("fal", fal.render)], checker=checker)

    return new_worker, value, api, calls, ledger


def check_pass(*args, **kwargs):
    return {"verdict": "pass", "issues": []}


def test_restart_recovers_exact_remote_job_and_checks_it_without_another_charge(pipeline, monkeypatch):
    new_worker, value, api, calls, ledger = pipeline

    def interrupted(request, timeout):
        if request.get_method() == "GET":
            raise KeyboardInterrupt
        return api(request, timeout)

    monkeypatch.setattr(fal, "_urlopen", interrupted)
    with closing(new_worker(check_pass)) as first:
        with pytest.raises(KeyboardInterrupt):
            first.generate(value)
    monkeypatch.setattr(fal, "_urlopen", api)
    monkeypatch.setenv("KLUKAI_FAL_DAILY_LIMIT", "0")
    monkeypatch.setenv("KLUKAI_FAL_MONTHLY_LIMIT", "0")
    with closing(new_worker(check_pass)) as resumed:
        status, image = resumed.recover(value)
        assert status == 200 and image.startswith(b"\x89PNG")
        before = list(calls)
        assert resumed.recover(value) == (status, image)
        assert calls == before
        assert resumed.db.execute("SELECT COUNT(*) FROM review_attempts").fetchone()[0] == 1
        assert resumed.db.execute("SELECT candidate FROM attempt_payloads").fetchone()[0] is None
    assert sum(method == "POST" for method, _ in calls) == 1
    with closing(sqlite3.connect(ledger)) as db:
        assert db.execute("SELECT COUNT(*) FROM fal_attempts").fetchone()[0] == 1


def test_restart_rechecks_saved_candidate_without_contacting_fal(pipeline, monkeypatch):
    new_worker, value, _, calls, _ = pipeline

    def interrupted_check(*args, **kwargs):
        raise KeyboardInterrupt

    with closing(new_worker(interrupted_check)) as first:
        with pytest.raises(KeyboardInterrupt):
            first.generate(value)
    monkeypatch.setattr(fal, "_urlopen", lambda *a, **k: pytest.fail("saved candidate needs no provider call"))
    with closing(new_worker(check_pass)) as resumed:
        assert resumed.recover(value)[0] == 200
        assert resumed.db.execute("SELECT COUNT(*) FROM review_attempts").fetchone()[0] == 2
    assert len(calls) == 3


def test_restart_after_review_pass_delivers_without_another_review_or_render(pipeline, monkeypatch):
    new_worker, value, _, _, _ = pipeline

    def interrupted_completion(*args):
        raise KeyboardInterrupt

    with closing(new_worker(check_pass)) as first:
        monkeypatch.setattr(first, "complete_attempt", interrupted_completion)
        with pytest.raises(KeyboardInterrupt):
            first.generate(value)
    monkeypatch.setattr(fal, "_urlopen", lambda *a, **k: pytest.fail("saved pass needs no provider call"))
    with closing(new_worker(lambda *a, **k: pytest.fail("approved candidate needs no new review"))) as resumed:
        assert resumed.recover(value)[0] == 200
        assert resumed.db.execute("SELECT COUNT(*) FROM review_attempts").fetchone()[0] == 1


def test_unknown_submission_without_remote_id_stays_pending_without_resubmission(pipeline, monkeypatch):
    new_worker, value, _, _, ledger = pipeline
    submissions = []

    def lost_response(request, timeout):
        submissions.append(request.get_method())
        raise http.client.IncompleteRead(b"partial")

    monkeypatch.setattr(fal, "_urlopen", lost_response)
    with closing(new_worker(check_pass)) as first:
        assert first.generate(value) == (409, {"status": "pending"})
    monkeypatch.setattr(fal, "fal_key", lambda: pytest.fail("missing remote ID cannot read a key"))
    with closing(new_worker(check_pass)) as resumed:
        assert resumed.recover(value) == (409, {"status": "pending"})
        assert resumed.generate(value) == (409, None)
    assert submissions == ["POST"]
    with closing(sqlite3.connect(ledger)) as db:
        assert db.execute("SELECT COUNT(*) FROM fal_attempts").fetchone()[0] == 1


@pytest.mark.parametrize("reply", [b"{}", b"[]", b"not JSON", b'{"request_id":"q1","status_url":"https://other.test/x"}'])
def test_untrustworthy_submission_reply_preserves_uncertain_work(pipeline, monkeypatch, reply):
    new_worker, value, _, _, ledger = pipeline
    calls = []

    def malformed(request, timeout):
        calls.append(request.get_method())
        return io.BytesIO(reply)

    monkeypatch.setattr(fal, "_urlopen", malformed)
    with closing(new_worker(check_pass)) as instance:
        assert instance.generate(value) == (409, {"status": "pending"})
        assert instance.recover(value) == (409, {"status": "pending"})
    assert calls == ["POST"]
    with closing(sqlite3.connect(ledger)) as db:
        assert db.execute("SELECT COUNT(*) FROM fal_attempts").fetchone()[0] == 1


def test_identical_retry_prompt_cannot_recover_an_earlier_attempt(pipeline, monkeypatch):
    new_worker, value, api, calls, _ = pipeline
    value = dict(value, prompt="Scene and appearance: Klukai is fully clothed; park.")
    monkeypatch.setenv("KLUKAI_FAL_DAILY_LIMIT", "2")
    monkeypatch.setenv("KLUKAI_FAL_MONTHLY_LIMIT", "2")
    submissions = []

    def refuse_then_lose_response(request, timeout):
        if request.get_method() == "POST":
            submissions.append(json.loads(request.data))
            if len(submissions) == 2:
                raise http.client.IncompleteRead(b"unknown second result")
            return api(request, timeout)
        if request.full_url.endswith("/status"):
            return api(request, timeout)
        calls.append(("GET", request.full_url))
        raise urllib.error.HTTPError(request.full_url, 422, "blocked", {},
                                     io.BytesIO(b'{"error_type":"content_policy_violation"}'))

    monkeypatch.setattr(fal, "_urlopen", refuse_then_lose_response)
    with closing(new_worker(check_pass)) as instance:
        assert instance.generate(value) == (409, {"status": "pending"})
        assert len(submissions) == 2 and submissions[0] == submissions[1]
        before = list(calls)
        assert instance.recover(value) == (409, {"status": "pending"})
        assert calls == before, "the second attempt has no handle; never observe the first job"
        payloads = [json.loads(row[0]) for row in instance.db.execute(
            "SELECT payload FROM attempt_payloads ORDER BY attempt_index")]
        assert payloads[0]["prompt"] == payloads[1]["prompt"]
        assert payloads[0]["_render_attempt"] != payloads[1]["_render_attempt"]
