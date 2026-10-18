#!/usr/bin/env python3
"""Private Unix-socket image worker for the account-authenticated Agy runtime."""
import argparse
import fcntl
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import signal
import socketserver
import sqlite3
import stat
import sys
import threading
import time
import uuid

from agy_art_runtime import ProviderUnavailable


MAX_BODY = 80 * 1024
MAX_IMAGE = 16 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
REQUEST_KEYS = {"request_id", "user_id", "prompt", "width", "height", "include_commander"}
# Core waits 380 s for one /generate. A rejected render is redone only while one
# more provider render (170 s) and its check still fit inside this budget.
REQUEST_BUDGET = 360
RENDER_RESERVE = 180
CHECK_TIMEOUT = 90


def private_directory(path):
    path = Path(path).absolute()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("invalid private directory")
    path.chmod(0o700)
    return path


def private_read(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > limit):
            raise ValueError("invalid private file")
        content = stream.read(limit + 1)
        if len(content) > limit:
            raise ValueError("private file too large")
        return content


def exclusive_lock(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("invalid worker lock")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def load_references(path):
    references = json.loads(private_read(path, MAX_BODY), object_pairs_hook=unique_object)
    if type(references) is not dict or set(references) != {"klukai", "commander"}:
        raise ValueError("invalid reference manifest")
    for reference in references.values():
        if (type(reference) is not dict or set(reference) != {"path", "sha256"}
                or type(reference["path"]) is not str
                or not Path(reference["path"]).is_absolute()
                or type(reference["sha256"]) is not str
                or not re.fullmatch(r"[a-f0-9]{64}", reference["sha256"])):
            raise ValueError("invalid reference manifest")
    return references


def validate_request(value):
    if type(value) is not dict or set(value) != REQUEST_KEYS:
        raise ValueError("invalid request")
    if type(value["request_id"]) is not str or len(value["request_id"]) != 36:
        raise ValueError("invalid request")
    request_id = str(uuid.UUID(value["request_id"]))
    if request_id != value["request_id"].lower():
        raise ValueError("invalid request")
    if type(value["user_id"]) is not str or not 1 <= len(value["user_id"]) <= 128:
        raise ValueError("invalid request")
    if (type(value["prompt"]) is not str or not 1 <= len(value["prompt"]) <= 16000
            or not value["prompt"].strip()):
        raise ValueError("invalid request")
    for name in ("width", "height"):
        if type(value[name]) is not int or not 1 <= value[name] <= 2048:
            raise ValueError("invalid request")
    if value["width"] * value["height"] > 12_000_000 or type(value["include_commander"]) is not bool:
        raise ValueError("invalid request")
    value["request_id"] = request_id
    # Surrogate code points have no valid UTF-8 representation for the runtime.
    value["prompt"].encode("utf-8")
    value["user_id"].encode("utf-8")
    return value


def request_digest(value):
    identity = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(identity).hexdigest()


def valid_image(data):
    # The renderer fully decodes and normalizes its output; enforce the transport
    # contract here as well, including before replaying stored output.
    return type(data) is bytes and data.startswith(PNG_SIGNATURE) and len(data) <= MAX_IMAGE


class Worker:
    def __init__(self, state_dir, token, references, allowed_users, renderer=None, *, providers=None,
                 checker=None):
        self.token = token
        # checker(output, value, references, timeout=) -> {"verdict": pass|reject|unchecked, ...}
        self.checker = checker
        self.references = references
        self.allowed_users = frozenset(allowed_users)
        if providers is not None and renderer is not None:
            raise ValueError("choose providers or legacy renderer")
        self.providers = validate_providers(providers if providers is not None else [("agy", renderer)])
        self.renderer = self.providers[0][1]
        self.slot = threading.BoundedSemaphore(1)
        self.db_lock = threading.Lock()
        database = state_dir / "requests.sqlite3"
        fd = os.open(database, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("invalid request database")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self.db = sqlite3.connect(database, check_same_thread=False)
        self.db.execute("PRAGMA synchronous = FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS requests ("
                        "request_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, "
                        "status TEXT NOT NULL, output BLOB)")
        # Add metadata without rewriting existing claims or cached image bytes.
        # Migration and initialization are serialized by the process state lock.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(requests)")}
        for column in ("preferred_provider", "selected_provider", "failure_category"):
            if column not in columns:
                self.db.execute(f"ALTER TABLE requests ADD COLUMN {column} TEXT")
        self.db.execute("CREATE TABLE IF NOT EXISTS worker_state (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        self.db.execute("INSERT OR IGNORE INTO worker_state VALUES ('rotation_next', 0)")
        self.db.execute("CREATE TABLE IF NOT EXISTS attempts ("
                        "request_id TEXT NOT NULL, attempt_index INTEGER NOT NULL, provider TEXT NOT NULL, "
                        "status TEXT NOT NULL, failure_category TEXT, PRIMARY KEY (request_id, attempt_index))")
        self.db.execute("CREATE TABLE IF NOT EXISTS checks ("
                        "request_id TEXT NOT NULL, attempt_index INTEGER NOT NULL, verdict TEXT NOT NULL, "
                        "issues TEXT NOT NULL, seconds REAL NOT NULL, PRIMARY KEY (request_id, attempt_index))")
        self.db.commit()

    def busy(self):
        acquired = self.slot.acquire(blocking=False)
        if acquired:
            self.slot.release()
        return not acquired

    def existing(self, request_id, request_hash):
        row = self.db.execute("SELECT request_hash, status, output FROM requests WHERE request_id = ?",
                              (request_id,)).fetchone()
        if row is None:
            return None
        if row[0] == request_hash and row[1] == "completed" and valid_image(row[2]):
            return 200, row[2]
        return 409, None

    def generate(self, value):
        if value["user_id"] not in self.allowed_users:
            return 403, None
        if value["include_commander"] and value["user_id"] != "jalsarraf":
            return 403, None
        digest = request_digest(value)
        with self.db_lock:
            prior = self.existing(value["request_id"], digest)
            if prior:
                return prior
            if not self.slot.acquire(blocking=False):
                return 429, None
            try:
                # Committed before the renderer can upload a reference or consume
                # a generation. Turn selection and the claim commit atomically:
                # busy/replay/invalid requests never consume a rotation turn.
                self.db.execute("BEGIN IMMEDIATE")
                turn = self.db.execute("SELECT value FROM worker_state WHERE key = 'rotation_next'").fetchone()[0]
                first = turn % len(self.providers)
                ordered = self.providers[first:] + self.providers[:first]
                preferred = ordered[0][0]
                self.db.execute("INSERT INTO requests (request_id, request_hash, status, preferred_provider, selected_provider) "
                                "VALUES (?, ?, 'pending', ?, ?)",
                                (value["request_id"], digest, preferred, preferred))
                self.db.execute("UPDATE worker_state SET value = value + 1 WHERE key = 'rotation_next'")
                self.db.execute("INSERT INTO attempts VALUES (?, 0, ?, 'pending', NULL)",
                                (value["request_id"], preferred))
                self.db.commit()
            except BaseException:
                self.db.rollback()
                self.slot.release()
                raise
        started = time.monotonic()
        try:
            index = 0
            for position, (provider, renderer) in enumerate(ordered):
                request = value
                redone = False
                while True:
                    if index:
                        # Each later attempt gets its own durable attribution
                        # before its native call; interrupted jobs remain pending.
                        with self.db_lock:
                            self.db.execute("INSERT INTO attempts VALUES (?, ?, ?, 'pending', NULL)",
                                            (value["request_id"], index, provider))
                            self.db.execute("UPDATE requests SET selected_provider = ? WHERE request_id = ?",
                                            (provider, value["request_id"]))
                            self.db.commit()
                    try:
                        output = renderer(request, self.references)
                        if not valid_image(output):
                            raise ValueError("invalid generated image")
                    except Exception as error:
                        eligible = isinstance(error, ProviderUnavailable)
                        timeout = "timeout" in str(error).lower() or "timed out" in str(error).lower()
                        category = "timeout" if timeout else ("unavailable" if eligible else "generation_failed")
                        terminal = not eligible or position + 1 == len(ordered)
                        self.fail_attempt(value["request_id"], index, category, terminal)
                        if terminal:
                            return (504 if timeout else 502), None
                        index += 1
                        break
                    verdict = self.check(output, value, index, started)
                    if verdict["verdict"] != "reject":
                        with self.db_lock:
                            self.db.execute("UPDATE attempts SET status = 'completed' WHERE request_id = ? AND attempt_index = ?",
                                            (value["request_id"], index))
                            self.db.execute("UPDATE requests SET status = 'completed', output = ? WHERE request_id = ?",
                                            (output, value["request_id"]))
                            self.db.commit()
                        return 200, output
                    # One re-render with the same provider, told what to avoid,
                    # and only while it and its check still fit the core deadline.
                    retry = (not redone and REQUEST_BUDGET - (time.monotonic() - started)
                             >= RENDER_RESERVE + CHECK_TIMEOUT)
                    self.fail_attempt(value["request_id"], index, "check_rejected", not retry)
                    if not retry:
                        return 502, None
                    redone = True
                    index += 1
                    from claude_image_check import fix_hint
                    request = dict(value, prompt=value["prompt"] + fix_hint(verdict["issues"]))
            return 502, None
        finally:
            self.slot.release()

    def fail_attempt(self, request_id, index, category, terminal):
        with self.db_lock:
            self.db.execute("UPDATE attempts SET status = 'failed', failure_category = ? "
                            "WHERE request_id = ? AND attempt_index = ?",
                            (category, request_id, index))
            if terminal:
                self.db.execute("UPDATE requests SET status = 'failed', failure_category = ? WHERE request_id = ?",
                                (category, request_id))
            self.db.commit()

    def check(self, output, value, index, started):
        """Run the configured checker; with none, or on any checker failure, deliver unchecked."""
        if self.checker is None:
            return {"verdict": "unchecked", "issues": []}
        remaining = REQUEST_BUDGET - (time.monotonic() - started) - 5
        try:
            verdict = self.checker(output, value, self.references, timeout=min(CHECK_TIMEOUT, remaining))
            if type(verdict) is not dict or verdict.get("verdict") not in {"pass", "reject", "unchecked"}:
                raise ValueError("invalid checker verdict")
        except Exception:
            verdict = {"verdict": "unchecked", "issues": []}
        issues = verdict.get("issues") if isinstance(verdict.get("issues"), list) else []
        seconds = verdict.get("seconds", 0.0)
        seconds = float(seconds) if type(seconds) in (int, float) else 0.0
        try:
            issues_text = json.dumps(issues)[:4096]
        except (TypeError, ValueError):
            issues_text = "[]"
        with self.db_lock:
            self.db.execute("INSERT OR REPLACE INTO checks VALUES (?, ?, ?, ?, ?)",
                            (value["request_id"], index, verdict["verdict"], issues_text, seconds))
            self.db.commit()
        return {"verdict": verdict["verdict"], "issues": issues}

    def recover(self, value):
        """Read one existing claim. Never claims, rotates, renders, or fails over."""
        if value["user_id"] not in self.allowed_users:
            return 403, {"status": "refused"}
        if value["include_commander"] and value["user_id"] != "jalsarraf":
            return 403, {"status": "refused"}
        digest = request_digest(value)
        with self.db_lock:
            row = self.db.execute(
                "SELECT request_hash, status, output, failure_category FROM requests WHERE request_id = ?",
                (value["request_id"],),
            ).fetchone()
        if row is None:
            return 404, {"status": "unknown"}
        if row[0] != digest:
            return 409, {"status": "conflict"}
        if row[1] == "completed" and valid_image(row[2]):
            return 200, row[2]
        if row[1] == "pending":
            return 409, {"status": "pending"}
        if row[1] == "failed":
            return 409, {"status": "refused" if row[3] == "refused" else "failed"}
        return 409, {"status": "unavailable"}

    def close(self):
        self.db.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "KlukaiImageWorker"
    sys_version = ""

    def setup(self):
        self.request.settimeout(self.server.read_timeout)
        super().setup()

    def log_message(self, *args):
        # HTTP paths, request bodies, headers and provider errors stay private.
        pass

    def reply(self, status, data=None):
        self.close_connection = True
        if status == 200 and type(data) is bytes:
            content, content_type = data, "image/png"
        else:
            if data is None:
                data = {"error": {400: "invalid request", 401: "unauthorized", 403: "forbidden",
                                  404: "not found", 409: "request unavailable", 429: "busy",
                                  502: "generation failed", 504: "generation timed out"}.get(status, "request failed")}
            content = json.dumps(data, separators=(",", ":")).encode()
            content_type = "application/json"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def send_error(self, code, message=None, explain=None):
        self.reply(code)

    def authorized(self):
        values = self.headers.get_all("Authorization", [])
        expected = b"Bearer " + self.server.worker.token
        if len(values) != 1 or not hmac.compare_digest(values[0].encode("utf-8"), expected):
            self.reply(401)
            return False
        return True

    def handle_expect_100(self):
        if not self.authorized():
            return False
        # A bounded JSON body needs no expectation handshake. Refuse it before
        # accepting any bytes instead of letting stdlib send 100 before auth.
        self.reply(400)
        return False

    def do_GET(self):
        if not self.authorized():
            return
        if self.headers.get_all("Transfer-Encoding"):
            self.reply(400)
        elif self.path != "/health":
            self.reply(404)
        else:
            worker = self.server.worker
            self.reply(200, {"status": "ready", "provider": "agy", "busy": worker.busy(),
                             "providers": [name for name, _ in worker.providers], "rotation": "alternate",
                             "checker": getattr(worker.checker, "model", None) if worker.checker else None})

    def do_POST(self):
        if not self.authorized():
            return
        if self.path not in {"/generate", "/recover"}:
            self.reply(404)
            return
        lengths = self.headers.get_all("Content-Length", [])
        content_types = self.headers.get_all("Content-Type", [])
        if (self.headers.get_all("Transfer-Encoding") or len(lengths) != 1
                or not re.fullmatch(r"[0-9]+", lengths[0]) or len(lengths[0]) > 6
                or not 1 <= int(lengths[0]) <= MAX_BODY
                or len(content_types) != 1
                or content_types[0].split(";", 1)[0].strip().lower() != "application/json"):
            self.reply(400)
            return
        try:
            data = self.rfile.read(int(lengths[0]))
            if len(data) != int(lengths[0]):
                raise ValueError("incomplete body")
            value = validate_request(json.loads(data.decode("utf-8"), object_pairs_hook=unique_object))
        except (ValueError, UnicodeError, RecursionError, TimeoutError, OSError):
            self.reply(400)
            return
        try:
            operation = self.server.worker.recover if self.path == "/recover" else self.server.worker.generate
            status, output = operation(value)
        except Exception:
            # Durable claims remain non-retryable if database/cache handling fails.
            status, output = 502, None
        self.reply(status, output)


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = False
    block_on_close = True

    def handle_error(self, request, client_address):
        # Never print an exception containing prompt or provider data.
        pass

    def server_close(self):
        super().server_close()
        if getattr(self, "worker", None) is not None:
            self.worker.close()
            self.worker = None
        path = getattr(self, "socket_path", None)
        if path is not None:
            path.unlink(missing_ok=True)
            self.socket_path = None
        for fd in getattr(self, "lock_fds", []):
            os.close(fd)
        self.lock_fds = []


def validate_providers(providers):
    if (not isinstance(providers, (list, tuple)) or not 1 <= len(providers) <= 2
            or any(not isinstance(item, (tuple, list)) or len(item) != 2 for item in providers)):
        raise ValueError("invalid image providers")
    names = [item[0] for item in providers]
    if (any(type(name) is not str or name not in {"agy", "grok"} for name in names)
            or len(set(names)) != len(names) or any(not callable(item[1]) for item in providers)):
        raise ValueError("invalid image providers")
    return tuple(tuple(item) for item in providers)


def load_providers(names):
    # Validate the complete selection before importing any native provider.
    validate_providers([(name, lambda *_: None) for name in names])
    providers = []
    for name in names:
        if name == "agy":
            from agy_image_render import render
        else:
            from grok_image_render import render
        providers.append((name, render))
    return providers


def load_checker(name):
    if name == "none":
        return None
    import claude_image_check

    def checker(output, value, references, *, timeout):
        return claude_image_check.run(output, value, references, timeout=timeout)
    checker.model = claude_image_check.MODEL
    return checker


def create_server(runtime_dir, state_dir, references, allowed_users, *, renderer=None, providers=None, read_timeout=10,
                  checker=None):
    runtime_dir, state_dir = private_directory(runtime_dir), private_directory(state_dir)
    token = private_read(runtime_dir / "token", 1024).strip()
    if not re.fullmatch(rb"[\x21-\x7e]{32,256}", token):
        raise ValueError("invalid worker token")
    references = load_references(references)
    if (not allowed_users or any(type(user) is not str or not 1 <= len(user) <= 128 for user in allowed_users)
            or read_timeout <= 0):
        raise ValueError("invalid worker configuration")
    if providers is not None and renderer is not None:
        raise ValueError("choose providers or legacy renderer")
    if providers is None:
        providers = [("agy", renderer)] if renderer is not None else load_providers(["agy"])
    providers = validate_providers(providers)
    locks, worker, server = [], None, None
    try:
        locks.append(exclusive_lock(runtime_dir / ".worker.lock"))
        locks.append(exclusive_lock(state_dir / ".worker.lock"))
        socket_path = runtime_dir / "image.sock"
        if socket_path.exists() or socket_path.is_symlink():
            info = socket_path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("invalid worker socket")
            socket_path.unlink()
        worker = Worker(state_dir, token, references, allowed_users, providers=providers, checker=checker)
        server = UnixHTTPServer(str(socket_path), Handler)
        socket_path.chmod(0o600)
        server.worker = worker
        server.read_timeout = read_timeout
        server.socket_path = socket_path
        server.lock_fds = locks
        return server
    except BaseException:
        if server is not None:
            server.server_close()
        if worker is not None:
            worker.close()
        for fd in locks:
            os.close(fd)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--allowed-users", required=True)
    parser.add_argument("--providers", default="agy", help="Ordered providers: agy, grok, or agy,grok")
    parser.add_argument("--checker", default="none", choices=("none", "claude"),
                        help="claude: Claude Sonnet 5.5 (effort high) checks every render (subscription login)")
    args = parser.parse_args()
    try:
        server = create_server(args.runtime_dir, args.state_dir, args.references,
                               {user.strip() for user in args.allowed_users.split(",") if user.strip()},
                               providers=load_providers([name.strip() for name in args.providers.split(",")]),
                               checker=load_checker(args.checker))
    except Exception:
        print("Image worker configuration unavailable", file=sys.stderr)
        return 1
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
