"""Codex, the owner's second image provider, returns one new image or fails cleanly."""
import io
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))

import agy_art_runtime as agy  # noqa: E402
import codex_image_render as codex  # noqa: E402


def png(color='red'):
    out = io.BytesIO()
    Image.new('RGB', (16, 16), color).save(out, format='PNG')
    return out.getvalue()


REQUEST = {'prompt': 'a rooftop at night', 'width': 1024, 'height': 1024, 'user_id': 'claude',
           'include_commander': False}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    generated = tmp_path / 'generated_images'
    generated.mkdir()
    monkeypatch.setattr(codex, 'GENERATED', generated)
    monkeypatch.setattr(codex, 'codex_binary', lambda: '/usr/bin/codex')
    monkeypatch.setattr(codex, 'read_image', lambda path: png('blue'))
    calls = {}

    def run(outcome):
        def fake_run(args, **kwargs):
            calls['args'], calls['kwargs'] = args, kwargs
            return outcome(generated, args, kwargs)
        monkeypatch.setattr(codex.subprocess, 'run', fake_run)
        return calls

    return run


def completed(stdout='', stderr=''):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr=stderr)


def test_returns_the_session_image_and_removes_the_copy(harness):
    session = str(uuid.uuid4())

    def outcome(generated, args, kwargs):
        folder = generated / session
        folder.mkdir()
        (folder / 'exec-1.png').write_bytes(png('green'))
        return completed(stderr=f'session id: {session}\n')

    calls = harness(outcome)
    result = codex.render(REQUEST, {'klukai': 'ref'})
    assert result.startswith(b'\x89PNG')
    assert not (codex.GENERATED / session).exists()
    args = calls['args']
    assert args[:3] == ['/usr/bin/codex', 'exec', '--skip-git-repo-check']
    assert 'model_reasoning_effort="low"' in args
    assert args[-1] == '-'
    assert any(a.startswith('--image=') and a.endswith('reference-klukai.png') for a in args)
    assert 'BEGIN BRIEF\na rooftop at night\nEND BRIEF' in calls['kwargs']['input']
    assert calls['kwargs']['timeout'] == codex.TIMEOUT_SECONDS
    assert 'GH_TOKEN' not in calls['kwargs']['env']


def test_a_timeout_is_a_failover(harness):
    def outcome(generated, args, kwargs):
        raise subprocess.TimeoutExpired(args, kwargs['timeout'])

    harness(outcome)
    with pytest.raises(agy.ProviderUnavailable):
        codex.render(REQUEST, {'klukai': 'ref'})


@pytest.mark.parametrize('message', ['You have hit your usage limit', 'HTTP 429 Too Many Requests',
                                     'Not logged in. Run codex login'])
def test_service_trouble_is_a_failover(harness, message):
    harness(lambda generated, args, kwargs: completed(stderr=message))
    with pytest.raises(agy.ProviderUnavailable):
        codex.render(REQUEST, {'klukai': 'ref'})


def test_no_image_is_a_generation_failure(harness):
    session = str(uuid.uuid4())
    harness(lambda generated, args, kwargs: completed(stderr=f'session id: {session}\nI cannot do that.'))
    with pytest.raises(agy.RuntimeError) as exc:
        codex.render(REQUEST, {'klukai': 'ref'})
    assert not isinstance(exc.value, agy.ProviderUnavailable)


def test_two_images_are_refused(harness):
    session = str(uuid.uuid4())

    def outcome(generated, args, kwargs):
        folder = generated / session
        folder.mkdir()
        (folder / 'a.png').write_bytes(png('green'))
        (folder / 'b.png').write_bytes(png('red'))
        return completed(stderr=f'session id: {session}\n')

    harness(outcome)
    with pytest.raises(agy.RuntimeError):
        codex.render(REQUEST, {'klukai': 'ref'})


def test_returning_the_reference_is_refused(harness):
    session = str(uuid.uuid4())

    def outcome(generated, args, kwargs):
        folder = generated / session
        folder.mkdir()
        (folder / 'exec-1.png').write_bytes(png('blue'))  # the reference itself
        return completed(stderr=f'session id: {session}\n')

    harness(outcome)
    with pytest.raises(agy.RuntimeError):
        codex.render(REQUEST, {'klukai': 'ref'})


def test_commander_reference_is_owner_only(harness):
    harness(lambda generated, args, kwargs: completed())
    with pytest.raises(agy.RuntimeError):
        codex.render(dict(REQUEST, include_commander=True), {'klukai': 'k', 'commander': 'c'})
