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
import uuid


MAX_BODY = 80 * 1024
MAX_IMAGE = 16 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
REQUEST_KEYS = {"request_id", "user_id", "prompt", "width", "height", "include_commander"}


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


def valid_image(data):
    # The renderer fully decodes and normalizes its output; enforce the transport
    # contract here as well, including before replaying stored output.
    return type(data) is bytes and data.startswith(PNG_SIGNATURE) and len(data) <= MAX_IMAGE


class Worker:
    def __init__(self, state_dir, token, references, allowed_users, renderer):
        self.token = token
        self.references = references
        self.allowed_users = frozenset(allowed_users)
        self.renderer = renderer
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
        identity = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        digest = hashlib.sha256(identity).hexdigest()
        with self.db_lock:
            prior = self.existing(value["request_id"], digest)
            if prior:
                return prior
            if not self.slot.acquire(blocking=False):
                return 429, None
            try:
                # Committed before the renderer can upload a reference or consume
                # a generation. Pending survives crashes and is never retried.
                self.db.execute("INSERT INTO requests VALUES (?, ?, 'pending', NULL)",
                                (value["request_id"], digest))
                self.db.commit()
            except BaseException:
                self.db.rollback()
                self.slot.release()
                raise
        try:
            try:
                output = self.renderer(value, self.references)
                if not valid_image(output):
                    raise ValueError("invalid generated image")
            except Exception as error:
                with self.db_lock:
                    self.db.execute("UPDATE requests SET status = 'failed' WHERE request_id = ?",
                                    (value["request_id"],))
                    self.db.commit()
                timeout = "timeout" in str(error).lower() or "timed out" in str(error).lower()
                return (504 if timeout else 502), None
            with self.db_lock:
                self.db.execute("UPDATE requests SET status = 'completed', output = ? WHERE request_id = ?",
                                (output, value["request_id"]))
                self.db.commit()
            return 200, output
        finally:
            self.slot.release()

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
            self.reply(200, {"status": "ready", "provider": "agy", "busy": self.server.worker.busy()})

    def do_POST(self):
        if not self.authorized():
            return
        if self.path != "/generate":
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
            status, output = self.server.worker.generate(value)
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


def create_server(runtime_dir, state_dir, references, allowed_users, *, renderer=None, read_timeout=10):
    runtime_dir, state_dir = private_directory(runtime_dir), private_directory(state_dir)
    token = private_read(runtime_dir / "token", 1024).strip()
    if not re.fullmatch(rb"[\x21-\x7e]{32,256}", token):
        raise ValueError("invalid worker token")
    references = load_references(references)
    if (not allowed_users or any(type(user) is not str or not 1 <= len(user) <= 128 for user in allowed_users)
            or read_timeout <= 0):
        raise ValueError("invalid worker configuration")
    if renderer is None:
        from agy_image_render import render
        renderer = render
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
        worker = Worker(state_dir, token, references, allowed_users, renderer)
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
    args = parser.parse_args()
    try:
        server = create_server(args.runtime_dir, args.state_dir, args.references,
                               {user.strip() for user in args.allowed_users.split(",") if user.strip()})
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
