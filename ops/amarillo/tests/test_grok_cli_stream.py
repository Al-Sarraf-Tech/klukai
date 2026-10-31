"""iter_stdout: the streaming CLI runner behind every chat and background turn.

A fake CLI (this interpreter running a tiny script) stands in for the pinned
binary, so these run without Grok, a login or the network.
"""
from pathlib import Path
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))

import grok_image_render as m  # noqa: E402

FAKE = r'''
import sys, time
mode = sys.argv[1]
if mode == "ok":
    print('{"type":"one"}', flush=True)
    print('{"type":"two"}', flush=True)
elif mode == "rate":
    print("HTTP 429: rate limit reached", file=sys.stderr, flush=True)
    sys.exit(3)
elif mode == "fail":
    print("something else broke", file=sys.stderr, flush=True)
    sys.exit(1)
elif mode == "flood":
    line = "x" * 1023
    for _ in range(64):
        print(line, flush=True)
    time.sleep(30)
elif mode == "linger":
    import os
    print('{"type":"one"}', flush=True)
    os.close(1)
    time.sleep(30)
elif mode == "slow":
    print('{"type":"one"}', flush=True)
    time.sleep(30)
'''


@pytest.fixture
def cli(tmp_path, monkeypatch):
    script = tmp_path / 'fake_grok.py'
    script.write_text(FAKE)
    auth = tmp_path / 'auth.json'
    auth.write_text('login')
    monkeypatch.setenv('GROK_AUTH_PATH', str(auth))
    work = tmp_path / 'work'
    work.mkdir()

    real_popen = m.subprocess.Popen
    started = []

    def popen(*args, **kwargs):
        started.append(real_popen(*args, **kwargs))
        return started[-1]

    monkeypatch.setattr(m.subprocess, 'Popen', popen)

    def run(mode, **kwargs):
        monkeypatch.setattr(m, 'command', lambda *a, **k: [sys.executable, str(script), mode])
        return m.iter_stdout(tmp_path, work, [], **kwargs)
    run.processes = started
    return run


def test_lines_arrive_in_order(cli):
    assert [line.strip() for line in cli('ok', timeout=10)] == ['{"type":"one"}', '{"type":"two"}']


def test_a_rate_limited_exit_is_unavailable_not_a_generic_failure(cli):
    with pytest.raises(m.agy.ProviderUnavailable):
        list(cli('rate', timeout=10))


def test_any_other_failed_exit_is_a_runtime_error(cli):
    with pytest.raises(m.agy.RuntimeError) as caught:
        list(cli('fail', timeout=10))
    assert not isinstance(caught.value, m.agy.ProviderUnavailable)


def test_output_is_capped_like_execute(cli, monkeypatch):
    monkeypatch.setattr(m, 'MAX_OUTPUT', 16 * 1024)
    started = time.monotonic()
    with pytest.raises(m.agy.RuntimeError, match='exceeded the limit'):
        list(cli('flood', timeout=20))
    assert time.monotonic() - started < 10


def test_a_cli_that_closes_stdout_but_lingers_is_killed(cli):
    started = time.monotonic()
    lines = cli('linger', timeout=20)
    assert next(lines).strip() == '{"type":"one"}'
    with pytest.raises(m.agy.RuntimeError, match='did not exit'):
        list(lines)
    assert time.monotonic() - started < 15


def test_the_deadline_kills_a_silent_cli(cli):
    with pytest.raises(m.agy.RuntimeError, match='timed out'):
        list(cli('slow', timeout=0.5))


def test_cancel_kills_the_cli_mid_stream(cli):
    cancel = threading.Event()
    lines = cli('slow', timeout=20, cancel=cancel)
    assert next(lines).strip() == '{"type":"one"}'
    cancel.set()
    with pytest.raises(m.agy.RuntimeError, match='cancelled'):
        list(lines)
    assert cli.processes[-1].poll() is not None


def test_a_consumer_that_stops_early_kills_the_cli(cli):
    lines = cli('slow', timeout=20)
    next(lines)
    lines.close()  # GeneratorExit inside the runner
    assert cli.processes[-1].poll() is not None
