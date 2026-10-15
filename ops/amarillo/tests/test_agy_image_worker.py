"""Exercise the private host API over real Unix HTTP with no cloud calls."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
        assert request(path, payload(), token="wrong")[0] == 401
        raw = raw_request(path, b"POST /generate HTTP/1.1\r\nHost: localhost\r\nContent-Length: 80000\r\n\r\n")
        assert b" 401 " in raw
        assert b"Connection: close" in raw
        status, headers, data = request(path, method="GET", target="/health")
        assert status == 200
        assert json.loads(data) == {
            "status": "ready", "provider": "agy", "busy": False,
            "providers": ["agy"], "rotation": "alternate",
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
        with sqlite3.connect(setup_files[1] / "requests.sqlite3") as db:
            row = db.execute("SELECT status FROM requests WHERE request_id = ?", (value["request_id"],)).fetchone()
        assert row == ("pending",)
        raise ValueError("simulate interruption")
    with running(worker, setup_files, render) as path:
        assert request(path, value)[0] == 502
    with sqlite3.connect(setup_files[1] / "requests.sqlite3") as db:
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
    with sqlite3.connect(setup_files[1] / "requests.sqlite3") as db:
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
        assert request(path, payload(), token="wrong")[0] == 401
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
    with sqlite3.connect(state / "requests.sqlite3") as db:
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
        with sqlite3.connect(setup_files[1] / "requests.sqlite3") as db:
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
