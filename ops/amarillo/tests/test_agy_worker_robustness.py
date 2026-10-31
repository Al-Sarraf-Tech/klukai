"""Worker admission regressions, using real gates without binding a socket."""
import importlib.util
import io
import json
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest


@pytest.fixture
def worker(monkeypatch):
    script = Path(__file__).resolve().parents[3] / "scripts" / "agy_image_worker.py"
    monkeypatch.syspath_prepend(str(script.parent))
    spec = importlib.util.spec_from_file_location("agy_worker_robustness", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_previous_call_cannot_cancel_the_next_background_holder(worker):
    gate = worker.ChatGate()
    assert gate.acquire(background=True, stopped=lambda: False)
    previous_cancel = gate.cancel_background
    gate.release()
    assert gate.acquire(background=True, stopped=lambda: False)
    try:
        # The old socket watcher may finish its disconnect check after release.
        previous_cancel.set()
        assert not gate.cancel_background.is_set()
    finally:
        gate.release()


def test_recall_takes_the_slot_before_waiting_extraction(worker):
    gate = worker.ChatGate()
    assert gate.acquire(background=True, stopped=lambda: False)
    active_cancel = gate.cancel_background
    waiting = threading.Event()
    recall_started = threading.Event()
    release_recall = threading.Event()
    abort = threading.Event()
    order = []

    def extract():
        def stopped():
            waiting.set()
            return abort.is_set()

        if not gate.acquire(background=True, stopped=stopped):
            return
        try:
            order.append("extraction")
        finally:
            gate.release()

    def recall():
        if not gate.acquire(background=False, stopped=abort.is_set):
            return
        try:
            order.append("recall")
            recall_started.set()
            assert release_recall.wait(2)
        finally:
            gate.release()

    released = False
    with ThreadPoolExecutor(max_workers=2) as pool:
        background = pool.submit(extract)
        foreground = None
        try:
            assert waiting.wait(2)
            foreground = pool.submit(recall)
            assert active_cancel.wait(2)
            gate.release()
            released = True
            assert recall_started.wait(2)
            assert order == ["recall"]
            release_recall.set()
            foreground.result(timeout=2)
            background.result(timeout=2)
            assert order == ["recall", "extraction"]
        finally:
            abort.set()
            release_recall.set()
            if not released:
                gate.release()


@pytest.mark.parametrize("background", [False, True])
def test_disconnected_waiter_releases_its_priority_claim(worker, background):
    gate = worker.ChatGate()
    assert gate.acquire(background=False, stopped=lambda: False)
    waiting = threading.Event()
    disconnected = threading.Event()

    def stopped():
        waiting.set()
        return disconnected.is_set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(gate.acquire, background=background, stopped=stopped)
        try:
            assert waiting.wait(2)
            disconnected.set()
            assert pending.result(timeout=2) is False
        finally:
            disconnected.set()
            gate.release()
    assert gate.acquire(background=True, stopped=lambda: False)
    gate.release()


@pytest.mark.parametrize("field,value", [
    ("role", []), ("role", {}), ("role", None), ("role", 1),
    ("effort", []), ("effort", {}), ("effort", True), ("effort", 1),
])
def test_non_string_role_and_effort_return_bad_request(worker, field, value):
    body = json.dumps({"system": "Klukai", "prompt": "Hello", field: value}).encode()
    handler = worker.Handler.__new__(worker.Handler)
    handler.headers = Message()
    handler.headers["Content-Length"] = str(len(body))
    handler.headers["Content-Type"] = "application/json"
    handler.rfile = io.BytesIO(body)
    replies = []
    handler.reply = lambda status, *args: replies.append(status)

    handler._complete_chat()

    assert replies == [400]


@pytest.mark.parametrize("role", ["chat", "recall", "background"])
def test_watcher_start_failure_releases_the_text_slot(worker, monkeypatch, role):
    def fail_start(_thread):
        raise RuntimeError("cannot start new thread")

    def forbidden(*args, **kwargs):
        pytest.fail("a failed request must not reach the CLI")

    monkeypatch.setattr(worker.threading.Thread, "start", fail_start)
    monkeypatch.setitem(sys.modules, "grok_account_chat", SimpleNamespace(
        stream=forbidden, effort_for=lambda role: "low",
    ))
    monkeypatch.setattr(worker, "client_gone", lambda conn: False)
    gates = SimpleNamespace(chat_gate=worker.ChatGate(), background_gate=worker.ChatGate())
    gate = gates.chat_gate if role == "chat" else gates.background_gate
    body = json.dumps({"prompt": "Hello", "role": role}).encode()
    handler = worker.Handler.__new__(worker.Handler)
    handler.headers = Message()
    handler.headers["Content-Length"] = str(len(body))
    handler.headers["Content-Type"] = "application/json"
    handler.rfile = io.BytesIO(body)
    handler.server = SimpleNamespace(worker=gates)
    handler.connection = object()
    replies = []
    handler.reply = lambda status, *args: replies.append(status)

    handler._complete_chat()

    assert replies == [502]
    deadline = time.monotonic() + 0.1
    assert gate.acquire(background=False, stopped=lambda: time.monotonic() >= deadline)
    gate.release()
