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

from agy_art_runtime import ContentRefused, GenerationPending, ProviderUnavailable, RuntimeError as RendererError
import implied_scene


MAX_BODY = 80 * 1024
COMPLETE_MAX_BODY = 1_000_000
MAX_IMAGE = 16 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
REQUEST_KEYS = {"request_id", "user_id", "prompt", "width", "height", "include_commander"}
# Owner decision 2026-10-19: core grants nudity of Klukai alone past the intimacy
# gate ("nudity": true, present only when granted: her own decision in the
# relationship). Owner standing rule 2026-10-23: it need not be tasteful; it
# stays adult, solo and without sex acts. The permission text remains for that
# rule. Owner decision 2026-10-30: the active Grok renderer refuses the flag
# before any reference, ledger or CLI work, the same as fal's renderer, and
# the grant is not a reason to skip the checker.
# Owner 2026-10-24: on fal, intimacy and nudity are implied only. Its filter
# refuses anything it reads as sexual and this account cannot lift it, so fal's
# scene is rewritten (implied_scene) and a grant becomes NUDITY_IMPLIED; a
# refusal is redrawn once, fully clothed. Those renders are checked as usual.
NUDITY_PROVIDERS = frozenset()
IMPLIED_PROVIDERS = frozenset({"fal"})
NUDITY_IMPLIED = (
    "\n\nKeep it implied: a towel, a bedsheet, steam or the framing covers her body; only her "
    "shoulders, arms and legs show. Klukai is an adult woman in her mid-twenties; never depict "
    "her as childlike."
)
NUDITY_PERMITTED = (
    "\n\nKlukai is an adult woman in her mid-twenties with a mature adult figure; never depict "
    "her as childlike. Where this scene calls for it, full, explicit nudity of Klukai alone is "
    "permitted. No sexual acts, no other people."
)
NUDITY_COVERED = "\n\nKeep Klukai covered, with a towel or swimwear in bathing scenes; no nudity."
# Core waits 380 s for one /generate. A rejected render is redone only while one
# more provider render (170 s) and its check still fit inside this budget.
REQUEST_BUDGET = 360
RENDER_RESERVE = 180
CHECK_TIMEOUT = 90
MAX_CHECK_ATTEMPTS = 2
RECOVERY_OBSERVE_TIMEOUT = 30.0
RECOVERY_BUDGET = RECOVERY_OBSERVE_TIMEOUT + MAX_CHECK_ATTEMPTS * CHECK_TIMEOUT + 5


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
    if type(value) is not dict or set(value) - {"nudity", "portrait_source"} != REQUEST_KEYS:
        raise ValueError("invalid request")
    # Only the literal grant, and never with the Commander's reference.
    if "nudity" in value and (value["nudity"] is not True or value["include_commander"] is not False):
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
    if "portrait_source" in value:
        from portrait_edit_input import decode_source
        decode_source(value["portrait_source"])
        if value["include_commander"] or "nudity" in value:
            raise ValueError("invalid portrait source")
    value["request_id"] = request_id
    # Surrogate code points have no valid UTF-8 representation for the runtime.
    value["prompt"].encode("utf-8")
    value["user_id"].encode("utf-8")
    return value


def provider_request(provider, value, *, plain=False):
    """What one provider sees: the brief plus its nudity clause, never the flag itself.

    fal gets the implied scene, or with plain=True the fully clothed redraw.
    """
    request = {key: item for key, item in value.items() if key != "nudity"}
    prompt = value["prompt"]
    if provider == "grok" and value.get("nudity") is True:
        # The renderer refuses this flag before references or the attempt ledger.
        request["nudity"] = True
        request["prompt"] = prompt
        return request
    if provider in IMPLIED_PROVIDERS:
        prompt = (implied_scene.plain if plain else implied_scene.implied)(prompt)
    if value.get("nudity") is True:
        if provider in NUDITY_PROVIDERS:
            prompt += NUDITY_PERMITTED
        elif provider in IMPLIED_PROVIDERS:
            # The plain redraw already dresses her.
            prompt += "" if plain else NUDITY_IMPLIED
        else:
            prompt += NUDITY_COVERED
    request["prompt"] = prompt
    return request


def request_digest(value):
    identity = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(identity).hexdigest()


def valid_image(data):
    # The renderer fully decodes and normalizes its output; enforce the transport
    # contract here as well, including before replaying stored output.
    return type(data) is bytes and data.startswith(PNG_SIGNATURE) and len(data) <= MAX_IMAGE


class Worker:
    def __init__(self, state_dir, token, references, allowed_users, renderer=None, *, providers=None,
                 checker=None, rotation="alternate"):
        if rotation not in ROTATIONS:
            raise ValueError("invalid provider rotation")
        # alternate: each new request starts with the next provider in turn.
        # fixed: every request starts with the first provider; the later ones
        # are only failovers (the owner keeps agy absolutely last).
        self.rotation = rotation
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
        self.db.execute("CREATE TABLE IF NOT EXISTS attempt_payloads ("
                        "request_id TEXT NOT NULL, attempt_index INTEGER NOT NULL, payload TEXT NOT NULL, "
                        "candidate BLOB, PRIMARY KEY (request_id, attempt_index))")
        self.db.execute("CREATE TABLE IF NOT EXISTS review_attempts ("
                        "request_id TEXT NOT NULL, attempt_index INTEGER NOT NULL, check_index INTEGER NOT NULL, "
                        "verdict TEXT NOT NULL, issues TEXT NOT NULL, seconds REAL NOT NULL, "
                        "PRIMARY KEY (request_id, attempt_index, check_index))")
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
        if "portrait_source" in value and any(name != "fal" for name, _ in self.providers):
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
                first = turn % len(self.providers) if self.rotation == "alternate" else 0
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
                plain = False
                request = provider_request(provider, value)
                redone = False
                while True:
                    if provider == "fal":
                        # Implied and fully clothed retries can have identical
                        # prompts. Bind recovery to this durable attempt, not
                        # just the request ID and creative payload.
                        request = dict(request, _render_attempt=index)
                    with self.db_lock:
                        # Each later attempt gets its own durable attribution
                        # before its native call; interrupted jobs remain pending.
                        if index:
                            self.db.execute("INSERT INTO attempts VALUES (?, ?, ?, 'pending', NULL)",
                                            (value["request_id"], index, provider))
                            self.db.execute("UPDATE requests SET selected_provider = ? WHERE request_id = ?",
                                            (provider, value["request_id"]))
                        # Store this exact transformed/refusal/redraw variant. Recovery
                        # must not rebuild it using changed prompts or provider rules.
                        self.db.execute("INSERT INTO attempt_payloads (request_id, attempt_index, payload) "
                                        "VALUES (?, ?, ?)",
                                        (value["request_id"], index, json.dumps(request)))
                        self.db.commit()
                    try:
                        output = renderer(request, self.references)
                        if not valid_image(output):
                            raise ValueError("invalid generated image")
                    except Exception as error:
                        if isinstance(error, GenerationPending):
                            # A paid request may still exist. Only observation may follow.
                            return 409, {"status": "pending"}
                        if (isinstance(error, ContentRefused) and provider in IMPLIED_PROVIDERS
                                and not plain and REQUEST_BUDGET - (time.monotonic() - started)
                                >= RENDER_RESERVE + CHECK_TIMEOUT):
                            # Refused before anything was drawn: the same moment once
                            # more, fully clothed, as its own attempt.
                            print(f"{provider} attempt {index} content_refused: {str(error)[:160]}; "
                                  "redrawing it fully clothed", file=sys.stderr, flush=True)
                            self.fail_attempt(value["request_id"], index, "content_refused", False)
                            plain = True
                            index += 1
                            request = provider_request(provider, value, plain=True)
                            continue
                        eligible = isinstance(error, ProviderUnavailable)
                        timeout = "timeout" in str(error).lower() or "timed out" in str(error).lower()
                        category = "timeout" if timeout else ("unavailable" if eligible else "generation_failed")
                        terminal = not eligible or position + 1 == len(ordered)
                        # Why, in the journal. Only the renderers' own errors carry their
                        # (fixed) message; anything else could quote the brief.
                        detail = str(error)[:160] if isinstance(error, RendererError) else "withheld"
                        print(f"{provider} attempt {index} {category}: {type(error).__name__}: {detail}",
                              file=sys.stderr, flush=True)
                        self.fail_attempt(value["request_id"], index, category, terminal)
                        if terminal:
                            return (504 if timeout else 502), None
                        index += 1
                        break
                    self.save_candidate(value["request_id"], index, output)
                    skip_check = self.checker is None or (
                        value.get("nudity") is True and provider in NUDITY_PROVIDERS)
                    if skip_check:
                        verdict = {"verdict": "unchecked", "issues": []}
                    else:
                        # Judged against what this provider was actually asked.
                        verdict = self.check(output, request, index, started)
                    if skip_check or verdict["verdict"] == "pass":
                        self.complete_attempt(value["request_id"], index, output)
                        return 200, output
                    if verdict["verdict"] == "unchecked":
                        # Review availability cannot authorize another paid render.
                        if self.review_count(value["request_id"], index) < MAX_CHECK_ATTEMPTS:
                            return 409, {"status": "pending"}
                        self.fail_attempt(value["request_id"], index, "check_unavailable", True)
                        return 502, None
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
                    request = provider_request(
                        provider, dict(value, prompt=value["prompt"] + fix_hint(verdict["issues"])),
                        plain=plain)
            return 502, None
        finally:
            self.slot.release()

    def save_candidate(self, request_id, index, output):
        with self.db_lock:
            self.db.execute("UPDATE attempt_payloads SET candidate = ? WHERE request_id = ? AND attempt_index = ?",
                            (output, request_id, index))
            self.db.commit()

    def complete_attempt(self, request_id, index, output):
        with self.db_lock:
            self.db.execute("UPDATE attempts SET status = 'completed', failure_category = NULL "
                            "WHERE request_id = ? AND attempt_index = ?", (request_id, index))
            self.db.execute("UPDATE requests SET status = 'completed', output = ?, failure_category = NULL "
                            "WHERE request_id = ?", (output, request_id))
            # Move ownership to the completed output atomically; do not keep a
            # second copy of the same image in the review cache indefinitely.
            self.db.execute("UPDATE attempt_payloads SET candidate = NULL "
                            "WHERE request_id = ? AND attempt_index = ?", (request_id, index))
            self.db.commit()

    def review_count(self, request_id, index):
        with self.db_lock:
            return self.db.execute("SELECT COUNT(*) FROM review_attempts "
                                   "WHERE request_id = ? AND attempt_index = ?",
                                   (request_id, index)).fetchone()[0]

    def fail_attempt(self, request_id, index, category, terminal):
        with self.db_lock:
            self.db.execute("UPDATE attempts SET status = 'failed', failure_category = ? "
                            "WHERE request_id = ? AND attempt_index = ?",
                            (category, request_id, index))
            if terminal:
                self.db.execute("UPDATE requests SET status = 'failed', failure_category = ? WHERE request_id = ?",
                                (category, request_id))
            self.db.commit()

    def check(self, output, value, index, started, *, budget=None):
        """Reserve at most two reviews per candidate, including interrupted calls."""
        if self.checker is None:
            return {"verdict": "unchecked", "issues": []}
        from claude_image_check import MIN_TIMEOUT, validate_verdict

        budget = REQUEST_BUDGET if budget is None else budget
        identity = (value["request_id"], index)
        with self.db_lock:
            saved = self.db.execute("SELECT verdict, issues FROM checks WHERE request_id = ? AND attempt_index = ?",
                                    identity).fetchone()
        if saved and saved[0] in {"pass", "reject"}:
            return validate_verdict({"verdict": saved[0], "issues": json.loads(saved[1])})
        verdict = {"verdict": "unchecked", "issues": []}
        for _ in range(MAX_CHECK_ATTEMPTS):
            remaining = budget - (time.monotonic() - started) - 5
            if remaining < MIN_TIMEOUT:
                break
            with self.db_lock:
                count = self.db.execute("SELECT COUNT(*) FROM review_attempts "
                                        "WHERE request_id = ? AND attempt_index = ?", identity).fetchone()[0]
                if count >= MAX_CHECK_ATTEMPTS:
                    break
                self.db.execute("INSERT INTO review_attempts VALUES (?, ?, ?, 'pending', '[]', 0)",
                                (*identity, count))
                self.db.commit()
            check_started = time.monotonic()
            try:
                result = self.checker(output, value, self.references, timeout=min(CHECK_TIMEOUT, remaining))
                verdict = validate_verdict(result)
            except Exception:
                verdict = {"verdict": "unchecked", "issues": []}
            seconds = round(time.monotonic() - check_started, 1)
            issues_text = json.dumps(verdict["issues"])
            with self.db_lock:
                self.db.execute("UPDATE review_attempts SET verdict = ?, issues = ?, seconds = ? "
                                "WHERE request_id = ? AND attempt_index = ? AND check_index = ?",
                                (verdict["verdict"], issues_text, seconds, *identity, count))
                self.db.execute("INSERT OR REPLACE INTO checks VALUES (?, ?, ?, ?, ?)",
                                (*identity, verdict["verdict"], issues_text, seconds))
                self.db.commit()
            if verdict["verdict"] != "unchecked":
                break
        return verdict

    def recover(self, value):
        """Observe paid work and resume bounded review; never render or fail over."""
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
        if row[1] == "failed":
            return 409, {"status": "refused" if row[3] == "refused" else "failed"}
        if row[1] != "pending":
            return 409, {"status": "unavailable"}
        if not self.slot.acquire(blocking=False):
            return 409, {"status": "pending"}
        try:
            # Re-read after taking the slot: another recovery may have completed
            # between the first lookup and acquisition.
            with self.db_lock:
                row = self.db.execute("SELECT status, output FROM requests WHERE request_id = ?",
                                      (value["request_id"],)).fetchone()
                attempt = self.db.execute(
                    "SELECT a.attempt_index, a.provider, a.status, a.failure_category, p.payload, p.candidate "
                    "FROM attempts a LEFT JOIN attempt_payloads p "
                    "ON a.request_id = p.request_id AND a.attempt_index = p.attempt_index "
                    "WHERE a.request_id = ? ORDER BY a.attempt_index DESC LIMIT 1",
                    (value["request_id"],)).fetchone()
            if row[0] == "completed" and valid_image(row[1]):
                return 200, row[1]
            if row[0] == "failed":
                return 409, {"status": "failed"}
            if not attempt or attempt[4] is None:
                # Legacy/unsupported interrupted requests have no recovery identity.
                return 409, {"status": "pending"}
            index, provider, status, failure, payload, output = attempt
            if status == "failed":
                self.fail_attempt(value["request_id"], index, failure or "generation_failed", True)
                return 409, {"status": "failed"}
            request = json.loads(payload)
            started = time.monotonic()
            if output is None:
                renderer = next((render for name, render in self.providers if name == provider), None)
                observer = getattr(renderer, "recover", None)
                if not callable(observer):
                    return 409, {"status": "pending"}
                try:
                    output = observer(request, self.references, timeout=RECOVERY_OBSERVE_TIMEOUT)
                except (GenerationPending, ProviderUnavailable):
                    return 409, {"status": "pending"}
                except Exception:
                    self.fail_attempt(value["request_id"], index, "generation_failed", True)
                    return 409, {"status": "failed"}
                if output is None:
                    return 409, {"status": "pending"}
                if valid_image(output):
                    self.save_candidate(value["request_id"], index, output)
            if not valid_image(output):
                self.fail_attempt(value["request_id"], index, "generation_failed", True)
                return 409, {"status": "failed"}
            skip_check = self.checker is None or (
                value.get("nudity") is True and provider in NUDITY_PROVIDERS)
            verdict = {"verdict": "unchecked", "issues": []} if skip_check else self.check(
                output, request, index, started, budget=RECOVERY_BUDGET)
            if skip_check or verdict["verdict"] == "pass":
                self.complete_attempt(value["request_id"], index, output)
                return 200, output
            if verdict["verdict"] == "reject":
                self.fail_attempt(value["request_id"], index, "check_rejected", True)
                return 409, {"status": "failed"}
            if self.review_count(value["request_id"], index) >= MAX_CHECK_ATTEMPTS:
                self.fail_attempt(value["request_id"], index, "check_unavailable", True)
                return 409, {"status": "failed"}
            return 409, {"status": "pending"}
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
            worker = self.server.worker
            self.reply(200, {"status": "ready", "provider": "agy", "busy": worker.busy(),
                             "providers": [name for name, _ in worker.providers], "rotation": worker.rotation,
                             "checker": getattr(worker.checker, "model", None) if worker.checker else None})

    def _complete_chat(self):
        lengths = self.headers.get_all("Content-Length", [])
        content_types = self.headers.get_all("Content-Type", [])
        if (self.headers.get_all("Transfer-Encoding") or len(lengths) != 1
                or not re.fullmatch(r"[0-9]+", lengths[0]) or len(lengths[0]) > 7
                or not 1 <= int(lengths[0]) <= COMPLETE_MAX_BODY
                or len(content_types) != 1
                or content_types[0].split(";", 1)[0].strip().lower() != "application/json"):
            self.reply(400)
            return
        try:
            data = self.rfile.read(int(lengths[0]))
            if len(data) != int(lengths[0]):
                raise ValueError("incomplete body")
            value = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object)
            if type(value) is not dict:
                raise ValueError("invalid chat")
            system, prompt = value.get("system", ""), value.get("prompt", "")
            if (type(system) is not str or type(prompt) is not str or not prompt.strip()
                    or len(system) > 800_000 or len(prompt) > 200_000):
                raise ValueError("invalid chat")
        except (ValueError, UnicodeError, RecursionError, TimeoutError, OSError, KeyError, AttributeError):
            self.reply(400)
            return
        if not self.server.worker.slot.acquire(blocking=False):
            self.reply(429)
            return
        try:
            import grok_account_chat
            turn = grok_account_chat.complete(system, prompt)
            self.reply(200, {"text": turn.text, "model": turn.model, "effort": turn.effort,
                             "effort_source": turn.effort_source})
        except Exception:
            self.reply(502, {"error": "grok chat failed"})
        finally:
            self.server.worker.slot.release()

    def do_POST(self):
        if not self.authorized():
            return
        if self.path == "/complete":
            self._complete_chat()
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


ROTATIONS = ("alternate", "fixed")
# Production is account-login Grok only. fal stays in the code as an opt-in
# sole provider (its own caps and key). It is never a failover behind Grok.
PROVIDER_NAMES = frozenset({"agy", "grok", "codex", "fal", "openai", "gemini"})


def validate_providers(providers):
    if (not isinstance(providers, (list, tuple)) or not 1 <= len(providers) <= len(PROVIDER_NAMES)
            or any(not isinstance(item, (tuple, list)) or len(item) != 2 for item in providers)):
        raise ValueError("invalid image providers")
    names = [item[0] for item in providers]
    if (any(type(name) is not str or name not in PROVIDER_NAMES for name in names)
            or len(set(names)) != len(names) or any(not callable(item[1]) for item in providers)):
        raise ValueError("invalid image providers")
    return tuple(tuple(item) for item in providers)


def load_providers(names):
    # Validate the complete selection before importing any native provider.
    if "fal" in names and list(names) != ["fal"]:
        raise ValueError("fal is opt-in only and is not a failover")
    validate_providers([(name, lambda *_: None) for name in names])
    providers = []
    for name in names:
        if name == "agy":
            from agy_image_render import render
        elif name == "codex":
            from codex_image_render import render
        elif name == "fal":
            from fal_image_render import render
        elif name == "openai":
            from cloud_image_render import render_openai as render
        elif name == "gemini":
            from cloud_image_render import render_gemini as render
        else:
            from grok_image_render import render
        providers.append((name, render))
    return providers


def load_checker(name):
    if name == "none":
        return None
    if name == "grok":
        import grok_image_check as checker_mod
    else:
        import claude_image_check as checker_mod

    def checker(output, value, references, *, timeout):
        return checker_mod.run(output, value, references, timeout=timeout)
    checker.model = checker_mod.MODEL
    return checker


def create_server(runtime_dir, state_dir, references, allowed_users, *, renderer=None, providers=None, read_timeout=10,
                  checker=None, rotation="alternate"):
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
        worker = Worker(state_dir, token, references, allowed_users, providers=providers, checker=checker,
                        rotation=rotation)
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
    parser.add_argument("--providers", default="grok",
                        help="Ordered providers: grok, agy, codex, openai, gemini, or fal alone. "
                             "Production is grok. fal cannot be combined with another provider.")
    parser.add_argument("--rotation", default="alternate", choices=ROTATIONS,
                        help="alternate: rotate the first provider per request; fixed: always start with the "
                             "first provider and use the rest only as failovers")
    parser.add_argument("--checker", default="grok", choices=("none", "grok", "claude"),
                        help="grok: Grok 4.7 (effort medium) checks every render through the account login. "
                             "claude remains available and is not the default.")
    args = parser.parse_args()
    try:
        server = create_server(args.runtime_dir, args.state_dir, args.references,
                               {user.strip() for user in args.allowed_users.split(",") if user.strip()},
                               providers=load_providers([name.strip() for name in args.providers.split(",")]),
                               checker=load_checker(args.checker), rotation=args.rotation)
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
