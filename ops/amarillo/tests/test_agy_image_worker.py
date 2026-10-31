"""Exercise the private host API over real Unix HTTP with no cloud calls."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import http.client
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import uuid

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts/agy_image_worker.py"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3ioAAAAASUVORK5CYII="
)
TOKEN = "test-private-token-" + "a" * 32


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    assert SCRIPT.exists(), "the authenticated image worker is not implemented"
    spec = importlib.util.spec_from_file_location("agy_image_worker", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=3)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def payload(**changes):
    value = {
        "request_id": str(uuid.uuid4()), "user_id": "jalsarraf",
        "prompt": "Klukai standing in a garden", "width": 832, "height": 1216,
        "include_commander": False,
    }
    value.update(changes)
    return value


def request(path, data=None, *, method="POST", target="/generate", token=TOKEN):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    connection = UnixConnection(path)
    try:
        connection.request(method, target, json.dumps(data) if data is not None else None, headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def one_write_status(path, data, *, token):
    """Status for a POST sent in one write.

    The worker refuses before reading the body (by design). http.client sends
    the body in a second write, which can race that close and fail with a
    broken pipe; one write over a unix socket cannot.
    """
    body = json.dumps(data).encode()
    head = (f"POST /generate HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode()
    return int(raw_request(path, head + body).split(b" ", 2)[1])


def raw_request(path, raw):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(str(path))
        connection.sendall(raw)
        chunks = []
        while block := connection.recv(65536):
            chunks.append(block)
        return b"".join(chunks)


@pytest.fixture
def setup_files():
    # Keep the full socket path below AF_UNIX's 108-byte limit.
    with tempfile.TemporaryDirectory(prefix="agy-worker-") as directory:
        root = Path(directory)
        runtime = root / "run"
        runtime.mkdir(mode=0o700)
        token = runtime / "token"
        token.write_text(TOKEN + "\n")
        token.chmod(0o600)
        references = root / "references.json"
        references.write_text(json.dumps({
            "klukai": {"path": str(root / "klukai.png"), "sha256": "a" * 64},
            "commander": {"path": str(root / "commander.png"), "sha256": "b" * 64},
        }))
        references.chmod(0o600)
        yield runtime, root / "state", references


@contextmanager
def running(worker, setup_files, render=None, **kwargs):
    def default_render(data, references):
        return PNG

    runtime, state, references = setup_files
    server = worker.create_server(
        runtime, state, references, {"jalsarraf", "test-user"},
        renderer=(render or default_render) if "providers" not in kwargs else None, **kwargs,
    )
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield runtime / "image.sock"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_health_and_generation_require_token_before_reading_body(worker, setup_files):
    with running(worker, setup_files) as path:
        assert request(path, method="GET", target="/health", token=None)[0] == 401
        assert one_write_status(path, payload(), token="wrong") == 401
        raw = raw_request(path, b"POST /generate HTTP/1.1\r\nHost: localhost\r\nContent-Length: 80000\r\n\r\n")
        assert b" 401 " in raw
        assert b"Connection: close" in raw
        status, headers, data = request(path, method="GET", target="/health")
        assert status == 200
        assert json.loads(data) == {
            "status": "ready", "provider": "agy", "busy": False,
            "providers": ["agy"], "rotation": "alternate", "checker": None,
        }
        assert TOKEN.encode() not in data


def test_success_uses_exact_request_and_fixed_manifest(worker, setup_files):
    received = []
    def render(data, references):
        received.append((data, references))
        return PNG

    value = payload(include_commander=True)
    with running(worker, setup_files, render) as path:
        status, headers, data = request(path, value)
        assert status == 200
        assert headers["Content-Type"] == "image/png"
        assert data == PNG
        assert received == [(value, json.loads(setup_files[2].read_text()))]
        assert path.stat().st_mode & 0o777 == 0o600
        assert setup_files[0].stat().st_mode & 0o777 == 0o700
        assert setup_files[1].stat().st_mode & 0o777 == 0o700
        for item in setup_files[1].rglob("*"):
            assert item.stat().st_mode & 0o077 == 0
        assert (setup_files[0] / "token").read_text().strip() == TOKEN


@pytest.mark.parametrize("change", [
    {"request_id": "not-a-uuid"}, {"request_id": 1},
    {"user_id": ""}, {"user_id": "x" * 129}, {"user_id": 7},
    {"prompt": ""}, {"prompt": "   "}, {"prompt": "x" * 16001}, {"prompt": []},
    {"width": True}, {"height": False}, {"width": 0}, {"height": -1},
    {"width": 2049}, {"height": 1.2}, {"include_commander": 1},
    {"reference_path": "/private/other-person.png"},
])
def test_invalid_contract_never_calls_renderer(worker, setup_files, change):
    def forbidden(*args):
        pytest.fail("invalid requests must never reach inference")
    with running(worker, setup_files, forbidden) as path:
        status, headers, data = request(path, payload(**change))
        assert status == 400
        assert headers["Connection"] == "close"
        assert b"private" not in data


def test_missing_keys_and_non_objects_are_invalid(worker, setup_files):
    with running(worker, setup_files) as path:
        value = payload()
        del value["width"]
        for malformed in [value, [], "prompt", None]:
            assert request(path, malformed)[0] == 400


def test_user_allowlist_and_owner_only_commander(worker, setup_files):
    def forbidden(*args):
        pytest.fail("forbidden requests must never reach inference")
    with running(worker, setup_files, forbidden) as path:
        assert request(path, payload(user_id="someone-else"))[0] == 403
        assert request(path, payload(user_id="test-user", include_commander=True))[0] == 403


@pytest.mark.parametrize("extra,body", [
    ("Content-Length: 81921\r\n", b""),
    ("Transfer-Encoding: chunked\r\n", b"0\r\n\r\n"),
    ("Content-Length: 2\r\nContent-Length: 2\r\n", b"{}"),
    ("Content-Length: -1\r\n", b""),
    ("Content-Length: 0\r\n", b""),
    ("Content-Length: 1\r\n", b"{"),
])
def test_rejects_unsupported_or_ambiguous_body_framing(worker, setup_files, extra, body):
    with running(worker, setup_files) as path:
        raw = (f"POST /generate HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {TOKEN}\r\n"
               f"Content-Type: application/json\r\n{extra}\r\n").encode() + body
        response = raw_request(path, raw)
        assert b" 400 " in response
        assert b"Connection: close" in response


def test_incomplete_body_times_out_without_claim_or_inference(worker, setup_files):
    with running(worker, setup_files, read_timeout=0.1) as path:
        raw = (f"POST /generate HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {TOKEN}\r\n"
               "Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{").encode()
        response = raw_request(path, raw)
        assert b" 400 " in response
        assert request(path, payload())[0] == 200


def test_duplicate_json_keys_are_rejected(worker, setup_files):
    with running(worker, setup_files) as path:
        body = json.dumps(payload())[:-1] + ', "user_id":"someone-else"}'
        raw = (f"POST /generate HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {TOKEN}\r\n"
               f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n{body}").encode()
        assert b" 400 " in raw_request(path, raw)


def test_health_rejects_transfer_encoding(worker, setup_files):
    with running(worker, setup_files) as path:
        raw = (f"GET /health HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {TOKEN}\r\n"
               "Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n").encode()
        assert b" 400 " in raw_request(path, raw)


def test_duplicate_auth_and_unauthenticated_expect_do_not_accept_body(worker, setup_files):
    with running(worker, setup_files) as path:
        raw = (f"POST /generate HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {TOKEN}\r\n"
               f"Authorization: Bearer {TOKEN}\r\nContent-Length: 100\r\n\r\n").encode()
        assert b" 401 " in raw_request(path, raw)
        response = raw_request(path, b"POST /generate HTTP/1.1\r\nHost: localhost\r\nExpect: 100-continue\r\nContent-Length: 100\r\n\r\n")
        assert b" 401 " in response
        assert b" 100 " not in response


def test_completed_request_replays_across_restart_and_rejects_changed_identity(worker, setup_files):
    values = []
    def render(data, references):
        values.append(data["request_id"])
        return PNG
    value = payload()
    with running(worker, setup_files, render) as path:
        assert request(path, value)[2] == PNG
        reordered = dict(reversed(list(value.items())))
        assert request(path, reordered)[2] == PNG
        for change in [{"prompt": "different"}, {"width": 1024}, {"user_id": "test-user"}, {"include_commander": True}]:
            assert request(path, dict(value, **change))[0] == 409
    with running(worker, setup_files, render) as path:
        assert request(path, value)[2] == PNG
    assert values == [value["request_id"]]


def test_busy_rejects_new_request_and_pending_duplicate_without_queue(worker, setup_files):
    started, release = threading.Event(), threading.Event()
    def render(data, references):
        started.set()
        assert release.wait(3)
        return PNG
    first, second = payload(), payload()
    with running(worker, setup_files, render) as path, ThreadPoolExecutor() as pool:
        pending = pool.submit(request, path, first)
        try:
            assert started.wait(2)
            assert request(path, first)[0] == 409
            assert request(path, second)[0] == 429
            assert json.loads(request(path, method="GET", target="/health")[2])["busy"] is True
        finally:
            release.set()
        assert pending.result()[0] == 200
        assert request(path, second)[0] == 200


@pytest.mark.parametrize("reason,status", [("secret prompt failed", 502), ("generation timed out secret", 504)])
def test_failed_claims_never_retry_and_errors_do_not_leak(worker, setup_files, reason, status, capsys):
    calls = []
    def render(data, references):
        calls.append(data["request_id"])
        raise ValueError(reason)
    value = payload()
    with running(worker, setup_files, render) as path:
        result = request(path, value)
        assert result[0] == status
        assert b"secret" not in result[2]
        assert request(path, value)[0] == 409
    with running(worker, setup_files, render) as path:
        assert request(path, value)[0] == 409
    assert calls == [value["request_id"]]
    assert "secret" not in capsys.readouterr().err


def test_pending_claim_is_committed_before_renderer_and_survives_restart(worker, setup_files):
    value = payload()
    def render(data, references):
        with closing(sqlite3.connect(setup_files[1] / "requests.sqlite3")) as db, db:
            row = db.execute("SELECT status FROM requests WHERE request_id = ?", (value["request_id"],)).fetchone()
        assert row == ("pending",)
        raise ValueError("simulate interruption")
    with running(worker, setup_files, render) as path:
        assert request(path, value)[0] == 502
    with closing(sqlite3.connect(setup_files[1] / "requests.sqlite3")) as db, db:
        db.execute("UPDATE requests SET status = 'pending'")
    with running(worker, setup_files) as path:
        assert request(path, value)[0] == 409


@pytest.mark.parametrize("output", [b"not png", PNG + b"x" * (16 * 1024 * 1024), "not bytes"])
def test_bad_or_oversize_output_is_failed_and_never_replayed(worker, setup_files, output):
    value = payload()
    with running(worker, setup_files, lambda *args: output) as path:
        assert request(path, value)[0] == 502
        assert request(path, value)[0] == 409


@pytest.mark.parametrize("kind", ["missing", "permissions", "symlink", "empty"])
def test_startup_requires_existing_private_token(worker, setup_files, kind):
    token = setup_files[0] / "token"
    if kind == "missing":
        token.unlink()
    elif kind == "permissions":
        token.chmod(0o644)
    elif kind == "symlink":
        source = token.with_name("source")
        token.rename(source)
        token.symlink_to(source)
    else:
        token.write_text("")
    with pytest.raises((ValueError, OSError)):
        worker.create_server(*setup_files, {"jalsarraf"}, renderer=lambda *args: PNG)


def test_second_worker_cannot_replace_live_socket_or_state(worker, setup_files):
    with running(worker, setup_files) as path:
        with pytest.raises((ValueError, OSError)):
            worker.create_server(*setup_files, {"jalsarraf"}, renderer=lambda *args: PNG)
        assert request(path, method="GET", target="/health")[0] == 200


@pytest.mark.parametrize("kind", ["permissions", "extra", "missing", "relative", "bad-digest"])
def test_startup_rejects_untrusted_reference_manifest(worker, setup_files, kind):
    reference_path = setup_files[2]
    references = json.loads(reference_path.read_text())
    if kind == "permissions":
        reference_path.chmod(0o644)
    elif kind == "extra":
        references["other-user"] = references["commander"]
    elif kind == "missing":
        references.pop("klukai")
    elif kind == "relative":
        references["commander"]["path"] = "./photo.png"
    else:
        references["klukai"]["sha256"] = "invalid"
    reference_path.write_text(json.dumps(references))
    with pytest.raises(ValueError):
        worker.create_server(*setup_files, {"jalsarraf"}, renderer=lambda *args: PNG)


def ledger(setup_files, query, parameters=()):
    with closing(sqlite3.connect(setup_files[1] / "requests.sqlite3")) as db, db:
        return db.execute(query, parameters).fetchall()


def named_renderers(calls):
    def render(name):
        def generate(value, references):
            calls.append((name, value["request_id"]))
            return PNG
        return generate
    return [(name, render(name)) for name in ("agy", "grok")]


def test_alternates_new_requests_and_persists_selected_provider(worker, setup_files):
    calls = []
    values = [payload() for _ in range(4)]
    with running(worker, setup_files, providers=named_renderers(calls)) as path:
        health = json.loads(request(path, method="GET", target="/health")[2])
        assert health["providers"] == ["agy", "grok"]
        assert health["rotation"] == "alternate"
        for value in values:
            assert request(path, value)[0] == 200
    assert calls == list(zip(["agy", "grok", "agy", "grok"], [v["request_id"] for v in values]))
    rows = ledger(setup_files, "SELECT preferred_provider, selected_provider, failure_category FROM requests ORDER BY rowid")
    assert rows == [(name, name, None) for name in ["agy", "grok", "agy", "grok"]]


def test_rotation_survives_restart_and_replay_or_invalid_requests_do_not_advance(worker, setup_files):
    calls = []
    first, second, third = payload(), payload(), payload()
    providers = named_renderers(calls)
    with running(worker, setup_files, providers=providers) as path:
        assert request(path, first)[0] == 200
        assert request(path, first)[2] == PNG
        assert request(path, dict(first, prompt="changed"))[0] == 409
        assert request(path, payload(width=False))[0] == 400
        assert request(path, payload(user_id="unauthorized"))[0] == 403
        assert one_write_status(path, payload(), token="wrong") == 401
    with running(worker, setup_files, providers=providers) as path:
        assert request(path, first)[2] == PNG
        assert request(path, second)[0] == 200
        assert request(path, third)[0] == 200
    assert calls == [("agy", first["request_id"]), ("grok", second["request_id"]), ("agy", third["request_id"])]


@pytest.mark.parametrize("failed_name", ["agy", "grok"])
def test_unavailable_provider_fails_over_once_in_both_directions(worker, setup_files, failed_name):
    from agy_art_runtime import ProviderUnavailable
    calls = []
    def render(name):
        def generate(value, references):
            calls.append(name)
            if name == failed_name:
                raise ProviderUnavailable("quota")
            return PNG
        return generate
    names = ["agy", "grok"] if failed_name == "agy" else ["grok", "agy"]
    value = payload()
    with running(worker, setup_files, providers=[(name, render(name)) for name in names]) as path:
        assert request(path, value)[2] == PNG
        assert request(path, value)[2] == PNG
    assert calls == names
    assert ledger(setup_files, "SELECT provider, status, failure_category FROM attempts ORDER BY attempt_index") == [
        (names[0], "failed", "unavailable"), (names[1], "completed", None),
    ]
    assert ledger(setup_files, "SELECT preferred_provider, selected_provider, status FROM requests") == [(names[0], names[1], "completed")]


@pytest.mark.parametrize("reason", ["policy denied", "invalid input", "invalid output", "secret timeout"])
def test_ordinary_errors_never_trigger_failover_and_failures_consume_turn(worker, setup_files, reason):
    calls = []
    def fail(value, references):
        calls.append("agy")
        raise ValueError(reason)
    def succeed(value, references):
        calls.append("grok")
        return PNG
    first, second = payload(), payload()
    providers = [("agy", fail), ("grok", succeed)]
    with running(worker, setup_files, providers=providers) as path:
        response = request(path, first)
        assert response[0] in {502, 504}
        assert reason.encode() not in response[2]
        assert calls == ["agy"]
        assert request(path, first)[0] == 409
    with running(worker, setup_files, providers=providers) as path:
        assert request(path, first)[0] == 409
        assert request(path, second)[0] == 200
    assert calls == ["agy", "grok"]
    assert ledger(setup_files, "SELECT COUNT(*) FROM attempts WHERE request_id = ?", (first["request_id"],)) == [(1,)]
    serialized = str(ledger(setup_files, "SELECT * FROM attempts"))
    assert reason not in serialized


@pytest.mark.parametrize("second_error,status", [("quota", 502), ("timeout", 504)])
def test_both_unavailable_attempt_only_twice_and_claim_remains_nonretryable(worker, setup_files, second_error, status):
    from agy_art_runtime import ProviderUnavailable
    calls = []
    def render(name, reason):
        def fail(value, references):
            calls.append(name)
            raise ProviderUnavailable(reason)
        return fail
    value = payload()
    providers = [("agy", render("agy", "quota")), ("grok", render("grok", second_error))]
    with running(worker, setup_files, providers=providers) as path:
        assert request(path, value)[0] == status
        assert request(path, value)[0] == 409
    with running(worker, setup_files, providers=providers) as path:
        assert request(path, value)[0] == 409
    assert calls == ["agy", "grok"]
    assert ledger(setup_files, "SELECT status FROM requests") == [("failed",)]


def test_busy_rejections_do_not_advance_rotation(worker, setup_files):
    started, release = threading.Event(), threading.Event()
    calls = []
    def slow(value, references):
        calls.append("agy")
        started.set()
        assert release.wait(3)
        return PNG
    def fast(value, references):
        calls.append("grok")
        return PNG
    first, second = payload(), payload()
    with running(worker, setup_files, providers=[("agy", slow), ("grok", fast)]) as path, ThreadPoolExecutor() as pool:
        pending = pool.submit(request, path, first)
        try:
            assert started.wait(2)
            assert request(path, first)[0] == 409
            assert request(path, second)[0] == 429
            assert request(path, payload())[0] == 429
        finally:
            release.set()
        assert pending.result()[0] == 200
        assert request(path, second)[0] == 200
    assert calls == ["agy", "grok"]


def test_rotation_and_attempt_are_committed_before_native_call(worker, setup_files):
    def render(value, references):
        assert ledger(setup_files, "SELECT value FROM worker_state WHERE key = 'rotation_next'") == [(1,)]
        assert ledger(setup_files, "SELECT preferred_provider, selected_provider, status FROM requests") == [("agy", "agy", "pending")]
        assert ledger(setup_files, "SELECT provider, status FROM attempts") == [("agy", "pending")]
        return PNG
    with running(worker, setup_files, providers=[("agy", render), ("grok", render)]) as path:
        assert request(path, payload())[0] == 200


def test_existing_database_migration_preserves_cache_and_ambiguous_claims(worker, setup_files):
    state = setup_files[1]
    state.mkdir(mode=0o700)
    values = [payload() for _ in range(3)]
    with closing(sqlite3.connect(state / "requests.sqlite3")) as db, db:
        db.execute("CREATE TABLE requests (request_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, status TEXT NOT NULL, output BLOB)")
        for value, status in zip(values, ["completed", "pending", "failed"]):
            digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
            db.execute("INSERT INTO requests VALUES (?, ?, ?, ?)", (value["request_id"], digest, status, PNG if status == "completed" else None))
    calls = []
    with running(worker, setup_files, providers=named_renderers(calls)) as path:
        assert request(path, values[0])[2] == PNG
        assert request(path, values[1])[0] == 409
        assert request(path, values[2])[0] == 409
        assert calls == []
        assert request(path, payload())[0] == 200
    assert len(calls) == 1 and calls[0][0] == "agy"
    assert ledger(setup_files, "SELECT COUNT(*) FROM requests") == [(4,)]


@pytest.mark.parametrize("names", [[], ["agy", "agy"], ["agy", "grok", "agy"], ["paid"], ["grok", "unknown"]])
def test_invalid_provider_configuration_is_rejected(worker, setup_files, names):
    with pytest.raises(ValueError):
        worker.create_server(*setup_files, {"jalsarraf"}, providers=[(name, lambda *args: PNG) for name in names])


def test_recover_reads_completed_bytes_and_does_not_render_unknown_pending_or_refusal(worker, setup_files):
    calls = []

    def render(value, references):
        calls.append(value["request_id"])
        return PNG

    done = payload()
    pending = payload()
    refused = payload(user_id="test-user", include_commander=True)
    missing = payload()
    with running(worker, setup_files, render=render) as path:
        assert request(path, done)[0] == 200
        digest = worker.request_digest(pending)
        with closing(sqlite3.connect(setup_files[1] / "requests.sqlite3")) as db, db:
            db.execute(
                "INSERT INTO requests (request_id, request_hash, status) VALUES (?, ?, 'pending')",
                (pending["request_id"], digest),
            )
        before = list(calls)
        assert request(path, done, target="/recover")[2] == PNG
        status, _headers, body = request(path, pending, target="/recover")
        assert status == 409 and json.loads(body)["status"] == "pending"
        status, _headers, body = request(path, missing, target="/recover")
        assert status == 404 and json.loads(body)["status"] == "unknown"
        status, _headers, body = request(path, refused, target="/recover")
        assert status == 403 and json.loads(body)["status"] == "refused"
        assert calls == before == [done["request_id"]]


def test_legacy_worker_constructor_remains_compatible(worker, setup_files):
    setup_files[1].mkdir(mode=0o700)
    app = worker.Worker(setup_files[1], TOKEN.encode(), {}, {"jalsarraf"}, renderer=lambda *args: PNG)
    try:
        assert app.generate(payload()) == (200, PNG)
    finally:
        app.close()


def test_provider_loading_imports_only_explicit_native_selection(worker, monkeypatch):
    import builtins
    native_import = builtins.__import__
    imports = []
    def guarded_import(name, *args, **kwargs):
        if name in {"agy_image_render", "grok_image_render"}:
            imports.append(name)
        return native_import(name, *args, **kwargs)
    fake_agy = lambda *args: PNG
    fake_grok = lambda *args: PNG
    monkeypatch.setitem(sys.modules, "agy_image_render", SimpleNamespace(render=fake_agy))
    monkeypatch.setitem(sys.modules, "grok_image_render", SimpleNamespace(render=fake_grok))
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert worker.load_providers(["agy"]) == [("agy", fake_agy)]
    assert imports == ["agy_image_render"]
    assert worker.load_providers(["grok", "agy"]) == [("grok", fake_grok), ("agy", fake_agy)]
    assert imports == ["agy_image_render", "grok_image_render", "agy_image_render"]
    with pytest.raises(ValueError):
        worker.load_providers(["agy", "paid"])
    assert imports == ["agy_image_render", "grok_image_render", "agy_image_render"]


def test_invalid_output_cannot_trigger_alternate_provider(worker, setup_files):
    calls = []
    def invalid(value, references):
        calls.append("agy")
        return b"broken png"
    def alternate(value, references):
        calls.append("grok")
        return PNG
    with running(worker, setup_files, providers=[("agy", invalid), ("grok", alternate)]) as path:
        assert request(path, payload())[0] == 502
    assert calls == ["agy"]


def checks_of(state, request_id):
    with closing(sqlite3.connect(state / "requests.sqlite3")) as db:
        return db.execute("SELECT attempt_index, verdict FROM checks WHERE request_id = ? ORDER BY attempt_index",
                          (request_id,)).fetchall()


def attempts_of(state, request_id):
    with closing(sqlite3.connect(state / "requests.sqlite3")) as db:
        return db.execute("SELECT attempt_index, provider, status, failure_category FROM attempts "
                          "WHERE request_id = ? ORDER BY attempt_index", (request_id,)).fetchall()


def scripted_checker(*verdicts, seen=None):
    queue = list(verdicts)

    def checker(output, value, references, *, timeout):
        if seen is not None:
            seen.append((output, value["prompt"], timeout))
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
    checker.model = "claude-sonnet-5-5"
    return checker


REJECT = {"verdict": "reject", "issues": [{"severity": "major", "what": "six fingers on her left hand"}]}


def test_health_names_the_checker(worker, setup_files):
    with running(worker, setup_files, checker=scripted_checker()) as path:
        assert json.loads(request(path, method="GET", target="/health")[2])["checker"] == "claude-sonnet-5-5"


def test_passing_check_delivers_and_is_recorded(worker, setup_files):
    seen = []
    with running(worker, setup_files, checker=scripted_checker({"verdict": "pass", "issues": []}, seen=seen)) as path:
        value = payload()
        status, _, data = request(path, value)
    assert (status, data) == (200, PNG)
    assert seen[0][0] == PNG and seen[0][1] == value["prompt"] and 0 < seen[0][2] <= 90
    assert checks_of(setup_files[1], value["request_id"]) == [(0, "pass")]


def test_rejected_render_is_redone_once_with_a_fix_hint(worker, setup_files):
    prompts = []

    def render(data, references):
        prompts.append(data["prompt"])
        return PNG
    checker = scripted_checker(REJECT, {"verdict": "pass", "issues": []})
    with running(worker, setup_files, providers=[("grok", render)], checker=checker) as path:
        value = payload()
        status, _, data = request(path, value)
    assert (status, data) == (200, PNG)
    assert prompts[0] == value["prompt"]
    assert prompts[1].startswith(value["prompt"]) and "six fingers on her left hand" in prompts[1]
    state = setup_files[1]
    assert attempts_of(state, value["request_id"]) == [
        (0, "grok", "failed", "check_rejected"), (1, "grok", "completed", None)]
    assert checks_of(state, value["request_id"]) == [(0, "reject"), (1, "pass")]


def test_two_rejections_fail_and_are_never_replayed(worker, setup_files):
    calls = []

    def render(data, references):
        calls.append(1)
        return PNG
    with running(worker, setup_files, providers=[("grok", render)],
                 checker=scripted_checker(REJECT, REJECT)) as path:
        value = payload()
        assert request(path, value)[0] == 502
        assert request(path, value)[0] == 409
    assert len(calls) == 2
    with closing(sqlite3.connect(setup_files[1] / "requests.sqlite3")) as db:
        assert db.execute("SELECT status, failure_category, output FROM requests WHERE request_id = ?",
                          (value["request_id"],)).fetchone() == ("failed", "check_rejected", None)


def test_no_redo_when_the_deadline_cannot_fit_another_render(worker, setup_files, monkeypatch):
    calls = []

    def render(data, references):
        calls.append(1)
        return PNG
    # One second short of room for another render plus its check.
    monkeypatch.setattr(worker, "REQUEST_BUDGET", worker.RENDER_RESERVE + worker.CHECK_TIMEOUT - 1)
    with running(worker, setup_files, providers=[("grok", render)], checker=scripted_checker(REJECT)) as path:
        assert request(path, payload())[0] == 502
    assert len(calls) == 1


@pytest.mark.parametrize("outcome", [RuntimeError("claude unavailable"), {"verdict": "maybe"}, None,
                                     {"verdict": "unchecked", "issues": []},
                                     {"verdict": "unchecked", "issues": [object()], "seconds": "soon"},
                                     {"verdict": "pass"}, {"verdict": "pass", "issues": [None]},
                                     {"verdict": "reject", "issues": []}])
def test_checker_failure_withholds_candidate_without_another_render(worker, setup_files, outcome):
    rendered, seen = [], []

    def render(data, references):
        rendered.append(data["request_id"])
        return PNG

    with running(worker, setup_files, render=render,
                 checker=scripted_checker(outcome, outcome, seen=seen)) as path:
        value = payload()
        status, _, data = request(path, value)
        assert status == 502 and data != PNG
        assert request(path, value)[0] == 409
        assert json.loads(request(path, value, target="/recover")[2])["status"] == "failed"
    assert rendered == [value["request_id"]]
    assert len(seen) == 2
    assert checks_of(setup_files[1], value["request_id"]) == [(0, "unchecked")]
    assert attempts_of(setup_files[1], value["request_id"]) == [(0, "agy", "failed", "check_unavailable")]


def test_checker_outage_rechecks_the_same_candidate_before_delivery(worker, setup_files, monkeypatch):
    rendered, seen, now = [], [], [0.0]
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def render(data, references):
        rendered.append(data["request_id"])
        return PNG

    def checker(output, value, references, *, timeout):
        seen.append((output, value["prompt"], timeout))
        if len(seen) == 1:
            now[0] = 300.0
            return {"verdict": "unchecked", "issues": []}
        return {"verdict": "pass", "issues": []}

    with running(worker, setup_files, render=render, checker=checker) as path:
        value = payload()
        status, _, data = request(path, value)
    assert (status, data) == (200, PNG)
    assert rendered == [value["request_id"]]
    assert seen == [(PNG, value["prompt"], 90), (PNG, value["prompt"], 55)]
    assert checks_of(setup_files[1], value["request_id"]) == [(0, "pass")]


def test_checker_outage_does_not_retry_after_review_budget_expires(worker, setup_files, monkeypatch):
    rendered, seen, now = [], [], [0.0]
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def render(data, references):
        rendered.append(data["request_id"])
        return PNG

    def checker(output, value, references, *, timeout):
        seen.append(output)
        now[0] = 341.0
        return {"verdict": "unchecked", "issues": []}

    with running(worker, setup_files, render=render, checker=checker) as path:
        value = payload()
        assert request(path, value)[0] == 409
    assert rendered == [value["request_id"]]
    assert seen == [PNG]
    assert attempts_of(setup_files[1], value["request_id"]) == [(0, "agy", "pending", None)]


def test_fixed_rotation_always_starts_with_the_first_provider(worker, setup_files):
    """The owner keeps agy absolutely last: with fixed rotation every new
    request starts with grok, and agy is never the first choice."""
    calls = []
    values = [payload() for _ in range(4)]

    def render(name):
        def generate(value, references):
            calls.append((name, value["request_id"]))
            return PNG
        return generate

    providers = [(name, render(name)) for name in ("grok", "agy")]
    with running(worker, setup_files, providers=providers, rotation="fixed") as path:
        health = json.loads(request(path, method="GET", target="/health")[2])
        assert health["providers"] == ["grok", "agy"]
        assert health["rotation"] == "fixed"
        for value in values:
            assert request(path, value)[0] == 200
    assert calls == [("grok", v["request_id"]) for v in values]
    rows = ledger(setup_files, "SELECT preferred_provider, selected_provider FROM requests ORDER BY rowid")
    assert rows == [("grok", "grok")] * 4


def test_fixed_rotation_uses_the_last_provider_only_as_a_failover(worker, setup_files):
    from agy_art_runtime import ProviderUnavailable
    calls = []

    def grok(value, references):
        calls.append("grok")
        raise ProviderUnavailable("grok service down")

    def agy(value, references):
        calls.append("agy")
        return PNG

    with running(worker, setup_files, providers=[("grok", grok), ("agy", agy)], rotation="fixed") as path:
        assert request(path, payload())[2] == PNG
        assert request(path, payload())[2] == PNG
    assert calls == ["grok", "agy", "grok", "agy"]


def test_an_unknown_rotation_is_refused(worker, setup_files):
    with pytest.raises(ValueError):
        with running(worker, setup_files, rotation="random"):
            pass


def test_granted_nudity_is_refused_by_grok_before_review(worker, setup_files):
    """The flag reaches the renderer, which refuses it; the checker never sees a render."""
    from agy_art_runtime import RuntimeError as RendererError

    seen_prompts, checked = [], []

    def grok(value, references):
        seen_prompts.append(value)
        raise RendererError("Nudity rendering is unsupported; provider safety checks stay enabled")

    value = payload(prompt="Klukai bathing in an onsen", nudity=True)
    with running(worker, setup_files, providers=[("grok", grok)],
                 checker=scripted_checker({"verdict": "pass", "issues": []}, seen=checked)) as path:
        assert request(path, value)[0] == 502
    (sent,) = seen_prompts
    assert sent["nudity"] is True
    assert worker.NUDITY_PERMITTED not in sent["prompt"]
    assert checked == []


def test_other_providers_get_the_covered_version_and_are_checked(worker, setup_files):
    from agy_art_runtime import ProviderUnavailable

    seen, checked = [], []

    def grok(value, references):
        seen.append(("grok", value["prompt"]))
        raise ProviderUnavailable("quota")

    def codex(value, references):
        seen.append(("codex", value["prompt"]))
        return PNG

    value = payload(prompt="Klukai bathing in an onsen", nudity=True)
    with running(worker, setup_files, providers=[("grok", grok), ("codex", codex)], rotation="fixed",
                 checker=scripted_checker({"verdict": "pass", "issues": []}, seen=checked)) as path:
        assert request(path, value)[0] == 200
    assert seen[1] == ("codex", value["prompt"] + worker.NUDITY_COVERED)
    assert worker.NUDITY_PERMITTED not in seen[1][1]
    assert len(checked) == 1 and checked[0][1].endswith(worker.NUDITY_COVERED)


def test_without_a_grant_prompts_are_unchanged(worker, setup_files):
    seen = []

    def grok(value, references):
        seen.append(value["prompt"])
        return PNG

    value = payload(prompt="Klukai bathing in an onsen")
    with running(worker, setup_files, providers=[("grok", grok)]) as path:
        assert request(path, value)[0] == 200
    assert seen == [value["prompt"]]


@pytest.mark.parametrize("change", [
    {"nudity": False}, {"nudity": "yes"}, {"nudity": 1},
    {"nudity": True, "include_commander": True},
])
def test_only_a_literal_grant_without_the_commander_is_accepted(worker, setup_files, change):
    calls = []

    def render(value, references):
        calls.append(value)
        return PNG

    with running(worker, setup_files, render) as path:
        assert request(path, payload(**change))[0] == 400
    assert calls == []


def test_a_failed_attempt_says_why_in_the_journal(worker, setup_files, capsys):
    """A signed-out Grok was invisible: the journal now names the provider,
    category and the renderer's own (fixed) message."""
    from agy_art_runtime import ProviderUnavailable

    def render(data, references):
        raise ProviderUnavailable("Grok account or image service is temporarily unavailable")
    with running(worker, setup_files, render) as path:
        assert request(path, payload())[0] in (502, 504)
    err = capsys.readouterr().err
    assert "unavailable: ProviderUnavailable: Grok account or image service" in err


def test_fal_is_an_accepted_sole_provider_and_not_a_failover():
    """fal stays opt-in. It cannot sit behind Grok as a paid failover."""
    import agy_image_worker as w
    assert w.validate_providers([("fal", lambda *_: None)])[0][0] == "fal"
    assert [name for name, _ in w.load_providers(["fal"])] == ["fal"]
    with pytest.raises(ValueError, match="not a failover"):
        w.load_providers(["grok", "fal"])


def test_optional_cloud_apis_load_only_when_explicitly_selected(worker, monkeypatch):
    from types import SimpleNamespace
    import sys

    openai = lambda *_: PNG
    gemini = lambda *_: PNG
    monkeypatch.setitem(sys.modules, "cloud_image_render",
                        SimpleNamespace(render_openai=openai, render_gemini=gemini))
    assert worker.load_providers(["openai", "gemini"]) == [("openai", openai), ("gemini", gemini)]
    assert [name for name, _ in worker.load_providers(["fal"])] == ["fal"]


SCENE_PROMPT = (
    "Create one polished anime illustration of the described scene.\n"
    "Scene and appearance: Klukai; couple; one man; one woman; hetero; shirtless; bare chest; "
    "medium breasts; black lace lingerie; lying on bed; seductive pose; kiss; intimate."
)


def test_granted_nudity_reaches_fal_only_implied_and_is_checked(worker, setup_files):
    """Owner 2026-10-24: on fal nudity is implied only; no API switch, so the render is checked."""
    seen, checked = [], []

    def fal(value, references):
        seen.append(value)
        return PNG

    value = payload(prompt="Klukai bathing in an onsen", nudity=True)
    with running(worker, setup_files, providers=[("fal", fal)],
                 checker=scripted_checker({"verdict": "pass", "issues": []}, seen=checked)) as path:
        assert request(path, value)[0] == 200
    (sent,) = seen
    assert "nudity" not in sent and sent["include_commander"] is False
    assert sent["prompt"] == value["prompt"] + worker.NUDITY_IMPLIED
    assert worker.NUDITY_PERMITTED not in sent["prompt"]
    assert len(checked) == 1 and checked[0][1] == sent["prompt"]
    for word in ("nude", "nudity", "naked", "explicit", "sexual"):
        assert word not in worker.NUDITY_IMPLIED.lower()


def test_fal_gets_the_implied_scene_and_the_checker_judges_that(worker, setup_files):
    seen, checked = [], []

    def fal(value, references):
        seen.append(value["prompt"])
        return PNG

    with running(worker, setup_files, providers=[("fal", fal)],
                 checker=scripted_checker({"verdict": "pass", "issues": []}, seen=checked)) as path:
        assert request(path, payload(prompt=SCENE_PROMPT, include_commander=True))[0] == 200
    (sent,) = seen
    for gone in ("hetero", "shirtless", "bare chest", "medium breasts", "lingerie", "seductive"):
        assert gone not in sent
    assert "kiss" in sent and "quilt" in sent and "tender" in sent
    assert checked[0][1] == sent


def test_a_fal_content_refusal_is_redrawn_once_fully_clothed(worker, setup_files, capsys):
    from agy_art_runtime import ContentRefused

    seen = []

    def fal(value, references):
        seen.append(value["prompt"])
        if len(seen) == 1:
            raise ContentRefused("fal refused the content (HTTP 422: content_policy_violation)")
        return PNG

    with running(worker, setup_files, providers=[("fal", fal)]) as path:
        assert request(path, payload(prompt=SCENE_PROMPT, include_commander=True))[2] == PNG
    first, second = seen
    assert "kiss" in first and "kiss" not in second and "lying on bed" not in second
    assert "fully clothed" in second and "side by side" in second
    assert "content_refused" in capsys.readouterr().err


def test_a_second_fal_refusal_fails_without_a_third_try(worker, setup_files):
    from agy_art_runtime import ContentRefused

    calls = []

    def fal(value, references):
        calls.append(value["prompt"])
        raise ContentRefused("fal refused the content (HTTP 422: content_policy_violation)")

    with running(worker, setup_files, providers=[("fal", fal)]) as path:
        assert request(path, payload(prompt=SCENE_PROMPT))[0] == 502
    assert len(calls) == 2


def test_other_providers_never_get_the_rewrite(worker, setup_files):
    seen = []

    def grok(value, references):
        seen.append(value["prompt"])
        return PNG

    with running(worker, setup_files, providers=[("grok", grok)]) as path:
        assert request(path, payload(prompt=SCENE_PROMPT))[0] == 200
    assert seen == [SCENE_PROMPT]


def test_fal_without_a_grant_never_sees_the_switch(worker, setup_files):
    seen = []

    def fal(value, references):
        seen.append(value)
        return PNG

    with running(worker, setup_files, providers=[("fal", fal)]) as path:
        assert request(path, payload(prompt="Klukai bathing in an onsen"))[0] == 200
    assert "nudity" not in seen[0]


def test_standing_rule_nudity_need_not_be_tasteful_but_stays_adult_and_solo():
    """Owner standing rule 2026-10-23: Klukai's nudity need not be tasteful.
    It stays adult, Klukai alone, and free of sexual acts."""
    import agy_image_worker as w
    text = w.NUDITY_PERMITTED
    assert "tasteful" not in text.lower() and "explicit sexual poses" not in text
    assert "full, explicit nudity of Klukai alone is permitted" in text
    for kept in ("adult woman in her mid-twenties", "never depict her as childlike", "No sexual acts",
                 "no other people"):
        assert kept in text


class InterruptedWorker(BaseException):
    """Simulate process death outside ordinary provider/checker exception handling."""


@contextmanager
def opened_worker(module, setup_files, render, *, checker=None, providers=None):
    runtime, state, references = setup_files
    server = module.create_server(
        runtime, state, references, {"jalsarraf", "test-user"},
        providers=providers or [("fal", render)], checker=checker, rotation="fixed")
    try:
        yield server.worker
    finally:
        server.server_close()


def test_uncertain_generation_remains_pending_without_failover(worker, setup_files):
    from agy_art_runtime import GenerationPending
    rendered = []

    def render(value, references):
        rendered.append(value)
        raise GenerationPending("accepted, observation interrupted")

    def alternate(*args):
        pytest.fail("uncertain paid work must not fail over")

    value = payload()
    with opened_worker(worker, setup_files, render,
                       providers=[("fal", render), ("grok", alternate)]) as current:
        assert current.generate(value)[0] == 409
        assert current.recover(value) == (409, {"status": "pending"})
        assert current.generate(value)[0] == 409
    assert len(rendered) == 1
    assert attempts_of(setup_files[1], value["request_id"]) == [(0, "fal", "pending", None)]


def test_candidate_and_review_reservation_survive_checker_interruption(worker, setup_files):
    rendered, checked = [], []
    value = payload()

    def render(request, references):
        rendered.append(request)
        return PNG

    def interrupted_check(output, request, references, *, timeout):
        checked.append(output)
        # A separate connection observes committed bytes and reservation before inference.
        with closing(sqlite3.connect(setup_files[1] / "requests.sqlite3")) as db:
            assert db.execute("SELECT candidate FROM attempt_payloads WHERE request_id = ?",
                              (value["request_id"],)).fetchone() == (PNG,)
            assert db.execute("SELECT COUNT(*) FROM review_attempts WHERE request_id = ?",
                              (value["request_id"],)).fetchone() == (1,)
        raise InterruptedWorker()

    with opened_worker(worker, setup_files, render, checker=interrupted_check) as current:
        with pytest.raises(InterruptedWorker):
            current.generate(value)
    with opened_worker(worker, setup_files, render,
                       checker=scripted_checker({"verdict": "pass", "issues": []}, seen=checked)) as current:
        assert current.generate(value)[0] == 409
        assert current.recover(value) == (200, PNG)
        assert current.recover(value) == (200, PNG)
        assert current.db.execute("SELECT COUNT(*) FROM review_attempts").fetchone() == (2,)
    assert len(rendered) == 1 and len(checked) == 2


def test_persisted_pass_completes_after_restart_without_rechecking(worker, setup_files, monkeypatch):
    rendered, checked = [], []
    value = payload()

    def render(request, references):
        rendered.append(request)
        return PNG

    def interrupt_completion(*args):
        raise InterruptedWorker()

    with opened_worker(worker, setup_files, render,
                       checker=scripted_checker({"verdict": "pass", "issues": []}, seen=checked)) as current:
        monkeypatch.setattr(current, "complete_attempt", interrupt_completion, raising=False)
        with pytest.raises(InterruptedWorker):
            current.generate(value)
    with opened_worker(worker, setup_files, render,
                       checker=lambda *a, **k: pytest.fail("a persisted pass must be reused")) as current:
        assert current.recover(value) == (200, PNG)
    assert len(rendered) == 1 and len(checked) == 1


def test_completion_keeps_one_image_copy_and_preserves_review_evidence(worker, setup_files):
    value = payload()
    with opened_worker(worker, setup_files, lambda *a: PNG,
                       checker=scripted_checker({"verdict": "pass", "issues": []})) as current:
        assert current.generate(value) == (200, PNG)
        assert current.db.execute("SELECT status, output FROM requests WHERE request_id = ?",
                                  (value["request_id"],)).fetchone() == ("completed", PNG)
        assert current.db.execute("SELECT candidate FROM attempt_payloads WHERE request_id = ?",
                                  (value["request_id"],)).fetchone() == (None,)
        assert current.db.execute("SELECT verdict FROM review_attempts WHERE request_id = ?",
                                  (value["request_id"],)).fetchall() == [("pass",)]
        assert current.recover(value) == (200, PNG)


def test_two_interrupted_reviews_exhaust_durable_budget(worker, setup_files):
    rendered, checked = [], []
    value = payload()

    def render(request, references):
        rendered.append(request)
        return PNG

    def interrupt(*args, **kwargs):
        checked.append(1)
        raise InterruptedWorker()

    with opened_worker(worker, setup_files, render, checker=interrupt) as current:
        with pytest.raises(InterruptedWorker):
            current.generate(value)
    with opened_worker(worker, setup_files, render, checker=interrupt) as current:
        with pytest.raises(InterruptedWorker):
            current.recover(value)
    with opened_worker(worker, setup_files, render, checker=interrupt) as current:
        assert current.recover(value) == (409, {"status": "failed"})
        assert current.recover(value) == (409, {"status": "failed"})
    assert len(rendered) == 1 and len(checked) == 2
    assert attempts_of(setup_files[1], value["request_id"]) == [(0, "fal", "failed", "check_unavailable")]


def test_saved_rejection_is_terminal_on_recovery_without_redraw(worker, setup_files, monkeypatch):
    rendered = []
    value = payload()

    def render(request, references):
        rendered.append(request)
        return PNG

    def interrupt(*args):
        raise InterruptedWorker()

    with opened_worker(worker, setup_files, render, checker=scripted_checker(REJECT)) as current:
        monkeypatch.setattr(current, "fail_attempt", interrupt)
        with pytest.raises(InterruptedWorker):
            current.generate(value)
    with opened_worker(worker, setup_files, render,
                       checker=lambda *a, **k: pytest.fail("saved reject must not be rechecked")) as current:
        assert current.recover(value) == (409, {"status": "failed"})
    assert len(rendered) == 1
    assert attempts_of(setup_files[1], value["request_id"]) == [(0, "fal", "failed", "check_rejected")]


@pytest.mark.parametrize("variant", ["plain_refusal", "quality_redraw"])
def test_recovery_observes_the_exact_transformed_attempt_payload(worker, setup_files, variant):
    from agy_art_runtime import ContentRefused, GenerationPending
    rendered, observed = [], []
    value = payload(prompt=SCENE_PROMPT, nudity=True)

    def render(request, references):
        rendered.append(dict(request))
        if len(rendered) == 1:
            if variant == "plain_refusal":
                raise ContentRefused("content refused")
            return PNG
        raise GenerationPending("accepted")

    def observe(request, references, *, timeout):
        observed.append((request, timeout))
        return PNG

    render.recover = observe
    with opened_worker(worker, setup_files, render, checker=scripted_checker(REJECT)) as current:
        assert current.generate(value)[0] == 409
    with opened_worker(worker, setup_files, render,
                       checker=scripted_checker({"verdict": "pass", "issues": []})) as current:
        assert current.recover(value) == (200, PNG)
    assert len(rendered) == 2
    assert rendered[1] != rendered[0]
    assert observed == [(rendered[1], 30.0)]
    assert "nudity" not in observed[0][0]


def test_recovery_observer_and_review_are_serialized_with_generation(worker, setup_files):
    from agy_art_runtime import GenerationPending
    entered, release = threading.Event(), threading.Event()
    rendered, observed, checked = [], [], []
    value = payload()

    def render(request, references):
        rendered.append(request)
        raise GenerationPending("accepted")

    def observe(request, references, *, timeout):
        observed.append(request)
        entered.set()
        assert release.wait(3)
        return PNG

    render.recover = observe
    with opened_worker(worker, setup_files, render,
                       checker=scripted_checker({"verdict": "pass", "issues": []}, seen=checked)) as current:
        assert current.generate(value)[0] == 409
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(current.recover, value)
            try:
                assert entered.wait(3)
                assert current.recover(value) == (409, {"status": "pending"})
                assert current.generate(payload())[0] == 429
            finally:
                release.set()
            assert future.result(timeout=3) == (200, PNG)
    assert len(rendered) == len(observed) == len(checked) == 1


def test_recovery_rejects_wrong_identity_before_observing_or_checking(worker, setup_files):
    from agy_art_runtime import GenerationPending
    value = payload()

    def render(request, references):
        raise GenerationPending("accepted")

    def forbidden(*args, **kwargs):
        pytest.fail("identity must be verified before external work")

    render.recover = forbidden
    with opened_worker(worker, setup_files, render, checker=forbidden) as current:
        assert current.generate(value)[0] == 409
        assert current.recover(dict(value, user_id="outsider"))[0] == 403
        assert current.recover(dict(value, user_id="test-user")) == (409, {"status": "conflict"})
        assert current.recover(dict(value, prompt="different")) == (409, {"status": "conflict"})
        assert current.recover(payload()) == (404, {"status": "unknown"})


@pytest.mark.parametrize("result", [None, "pending_error", "unavailable"])
def test_pending_observation_never_consumes_review_or_new_generation(worker, setup_files, result):
    from agy_art_runtime import GenerationPending, ProviderUnavailable
    rendered, observed = [], []
    value = payload()

    def render(request, references):
        rendered.append(request)
        raise GenerationPending("accepted")

    def observe(request, references, *, timeout):
        observed.append(request)
        if result == "pending_error":
            raise GenerationPending("still pending")
        if result == "unavailable":
            raise ProviderUnavailable("observer configuration temporarily unavailable")
        return None

    render.recover = observe
    with opened_worker(worker, setup_files, render,
                       checker=lambda *a, **k: pytest.fail("no candidate to check")) as current:
        assert current.generate(value)[0] == 409
        assert current.recover(value) == (409, {"status": "pending"})
        assert current.recover(value) == (409, {"status": "pending"})
        assert current.db.execute("SELECT COUNT(*) FROM review_attempts").fetchone() == (0,)
    assert len(rendered) == 1 and len(observed) == 2


def test_observed_candidate_survives_interruption_without_reobserving(worker, setup_files):
    from agy_art_runtime import GenerationPending
    rendered, observed, checked = [], [], []
    value = payload()

    def render(request, references):
        rendered.append(request)
        raise GenerationPending("accepted")

    def observe(request, references, *, timeout):
        observed.append(request)
        return PNG

    def interrupt(*args, **kwargs):
        checked.append(1)
        raise InterruptedWorker()

    render.recover = observe
    with opened_worker(worker, setup_files, render, checker=interrupt) as current:
        assert current.generate(value)[0] == 409
        with pytest.raises(InterruptedWorker):
            current.recover(value)
    render.recover = lambda *a, **k: pytest.fail("persisted bytes need no further provider observation")
    with opened_worker(worker, setup_files, render,
                       checker=scripted_checker({"verdict": "pass", "issues": []}, seen=checked)) as current:
        assert current.recover(value) == (200, PNG)
    assert len(rendered) == len(observed) == 1 and len(checked) == 2


def test_invalid_observed_candidate_is_terminal_without_review(worker, setup_files):
    from agy_art_runtime import GenerationPending
    observed = []
    value = payload()

    def render(request, references):
        raise GenerationPending("accepted")

    def observe(request, references, *, timeout):
        observed.append(request)
        return b"not an image"

    render.recover = observe
    with opened_worker(worker, setup_files, render,
                       checker=lambda *a, **k: pytest.fail("invalid image cannot be reviewed")) as current:
        assert current.generate(value)[0] == 409
        assert current.recover(value) == (409, {"status": "failed"})
        assert current.recover(value) == (409, {"status": "failed"})
        assert current.db.execute("SELECT COUNT(*) FROM review_attempts").fetchone() == (0,)
    assert len(observed) == 1


def test_candidate_with_no_remaining_request_time_can_be_reviewed_afterward(worker, setup_files, monkeypatch):
    now, rendered, checked = [0.0], [], []
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: now[0]))
    value = payload()

    def render(request, references):
        rendered.append(request)
        now[0] = 341.0
        return PNG

    with opened_worker(worker, setup_files, render,
                       checker=scripted_checker({"verdict": "pass", "issues": []}, seen=checked)) as current:
        assert current.generate(value)[0] == 409
        assert checked == []
        assert current.recover(value) == (200, PNG)
    assert len(rendered) == len(checked) == 1


@pytest.mark.parametrize("provider,grant", [("fal", False)])
def test_recovery_preserves_explicit_review_exceptions(worker, setup_files, provider, grant):
    from agy_art_runtime import GenerationPending
    value = payload(**({"nudity": True} if grant else {}))

    def render(request, references):
        raise GenerationPending("accepted")

    render.recover = lambda request, references, *, timeout: PNG
    checker = (lambda *a, **k: pytest.fail("existing grant must skip review")) if grant else None
    with opened_worker(worker, setup_files, render, providers=[(provider, render)], checker=checker) as current:
        assert current.generate(value)[0] == 409
        assert current.recover(value) == (200, PNG)
        assert current.db.execute("SELECT COUNT(*) FROM review_attempts").fetchone() == (0,)


def test_a_closed_caller_is_detected(worker):
    left, right = socket.socketpair()
    try:
        assert worker.client_gone(left) is False
        right.close()
        assert worker.client_gone(left) is True
    finally:
        left.close()


def test_chat_runs_right_after_a_slow_background_call(worker, setup_files, monkeypatch):
    started = threading.Event()
    background_done = threading.Event()
    result = {}

    class Turn:
        def __init__(self, text):
            self.text = text
            self.model, self.effort, self.effort_source = "grok-4.7", "medium", "stream"

    def complete(system, prompt, cancel=None):
        if prompt == "recall":
            started.set()
            while cancel is None or not cancel.is_set():
                time.sleep(0.01)
            raise RuntimeError("cancelled")
        return Turn("hello")

    monkeypatch.setitem(sys.modules, "grok_account_chat", SimpleNamespace(complete=complete))
    runtime, state, references = setup_files
    server = worker.create_server(runtime, state, references, {"jalsarraf", "test-user"})
    server.worker.slot.acquire()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    path = runtime / "image.sock"

    def run_background():
        result["background"] = request(
            path, {"system": "rank", "prompt": "recall", "role": "background"}, target="/complete",
        )
        background_done.set()

    background = threading.Thread(target=run_background)
    background.start()
    try:
        assert started.wait(2)
        status, _headers, body = request(
            path, {"system": "Be Klukai.", "prompt": "Say ready.", "role": "chat"}, target="/complete",
        )
        assert status == 200
        assert json.loads(body)["text"] == "hello"
        assert background_done.wait(2)
        assert result["background"][0] == 502
        assert request(
            path, {"system": "Be Klukai.", "prompt": "again", "role": "nope"}, target="/complete",
        )[0] == 400
    finally:
        server.worker.slot.release()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        background.join(timeout=3)


def test_complete_reports_grok_4_7_at_medium(worker, setup_files, monkeypatch):
    class Turn:
        text, model, effort, effort_source = "ready", "grok-4.7", "medium", "stream"

    monkeypatch.setitem(sys.modules, "grok_account_chat", SimpleNamespace(
        complete=lambda system, prompt, cancel=None: Turn()))
    with running(worker, setup_files) as path:
        status, _headers, body = request(path, {"system": "Be Klukai.", "prompt": "Say ready."},
                                          target="/complete")
    assert status == 200
    assert json.loads(body) == {
        "text": "ready", "model": "grok-4.7", "effort": "medium", "effort_source": "stream",
    }
