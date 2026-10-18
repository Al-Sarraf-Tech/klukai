"""Grok's bounded worker must not upload unapproved files or repeat edits."""
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))


def module(name='grok_image_render'):
    assert importlib.util.find_spec(name), 'Grok renderer is not implemented'
    return importlib.import_module(name)


def png(color='red'):
    out = io.BytesIO()
    Image.new('RGB', (16, 16), color).save(out, format='PNG')
    return out.getvalue()


def events(session, result=None):
    return [
        {'type': 'system', 'subtype': 'init', 'session_id': session, 'apiKeySource': 'oauth',
         'tools': ['image_edit'], 'mcp_servers': [], 'skills': [], 'model': 'grok-4.7'},
        {'type': 'assistant', 'session_id': session, 'parent_tool_use_id': None,
         'message': {'content': [{'type': 'tool_use', 'id': 'one', 'name': 'image_edit',
                                 'input': {'image': ['/work/klukai.png'], 'prompt': 'Draw'}}]}},
        {'type': 'user', 'session_id': session, 'parent_tool_use_id': None,
         'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'one',
                                 'is_error': False, 'content': result or 'Image saved'}]}},
        {'type': 'result', 'subtype': 'success', 'is_error': False,
         'stop_reason': 'end_turn', 'session_id': session},
    ]


def parse(rows, session):
    return module().parse_stream('\n'.join(map(json.dumps, rows)), session, {'/work/klukai.png'})


def test_gate_allows_one_edit_and_denies_retries_and_extra_references(tmp_path):
    gate = module('grok_image_gate')
    policy = {'images': ['/work/klukai.png']}
    call = {'toolName': 'image_edit', 'toolInput': {'image': ['/work/klukai.png'], 'prompt': 'Draw'}}
    assert gate.decide(call, policy, tmp_path/'claim') == 'allow'
    assert gate.decide(call, policy, tmp_path/'claim') == 'deny'
    call['toolInput']['image'] += ['/home/user/.grok/auth.json']
    assert gate.decide(call, policy, tmp_path/'other') == 'deny'
    assert not (tmp_path/'other').exists()


@pytest.mark.parametrize('name', ['image_gen', 'run_terminal_cmd', 'MCPTool', 'read_file', 'video_gen'])
def test_gate_denies_every_non_edit_tool(name, tmp_path):
    assert module('grok_image_gate').decide({'toolName': name, 'toolInput': {}},
                                          {'images': ['/work/klukai.png']}, tmp_path/'claim') == 'deny'


def test_malformed_or_truncated_tool_input_is_denied(tmp_path):
    g = module('grok_image_gate')
    for call in [None, {}, {'toolName': 'image_edit', 'toolInput': None},
                 {'toolName': 'image_edit', 'toolInputTruncated': True,
                  'toolInput': {'image': ['/work/klukai.png'], 'prompt': 'Draw'}}]:
        assert g.decide(call, {'images': ['/work/klukai.png']}, tmp_path/'claim') == 'deny'


def test_stream_requires_oauth_one_edit_and_completed_success():
    sid = str(uuid.uuid4())
    assert parse(events(sid), sid) is None
    for change in ['api', 'missing_result', 'duplicate', 'other_session', 'error', 'refusal', 'mcp']:
        rows = events(sid)
        if change == 'api': rows[0]['apiKeySource'] = 'user'
        if change == 'missing_result': rows.pop(2)
        if change == 'duplicate': rows.insert(2, rows[1])
        if change == 'other_session': rows[-1]['session_id'] = str(uuid.uuid4())
        if change == 'error': rows[2]['message']['content'][0]['is_error'] = True
        if change == 'refusal': rows[-1]['stop_reason'] = 'refusal'
        if change == 'mcp': rows[0]['mcp_servers'] = [{'name': 'unsafe', 'status': 'connected'}]
        with pytest.raises(module().agy.RuntimeError): parse(rows, sid)


def test_unverified_session_errors_distinguish_missing_terminal_and_auth_without_payloads():
    m = module(); sid = str(uuid.uuid4()); rows = events(sid)
    with pytest.raises(m.agy.RuntimeError, match='initial=1 terminal=0'):
        parse(rows[:-1], sid)
    rows[0]['apiKeySource'] = 'user'
    with pytest.raises(m.agy.RuntimeError, match='not OAuth'):
        parse(rows, sid)


@pytest.mark.parametrize('refusal', ['terminal', 'block', 'message'])
def test_refusal_anywhere_overrides_earlier_service_error(refusal):
    m = module(); sid = str(uuid.uuid4()); rows = events(sid, 'HTTP 500 Internal Server Error')
    rows[2]['message']['content'][0]['is_error'] = True
    if refusal == 'terminal': rows[-1]['stop_reason'] = 'refusal'
    if refusal == 'block':
        rows.insert(-1, {'type': 'assistant', 'session_id': sid,
                         'message': {'content': [{'type': 'refusal', 'refusal': 'Cannot comply'}]}})
    if refusal == 'message':
        rows.insert(-1, {'type': 'assistant', 'session_id': sid,
                         'message': {'stop_reason': 'refusal', 'content': []}})
    with pytest.raises(m.agy.RuntimeError) as error: parse(rows, sid)
    assert not isinstance(error.value, m.agy.ProviderUnavailable)


@pytest.mark.parametrize('refused', [False, True])
def test_string_error_message_preserves_service_classification_and_refusal_precedence(refused):
    m = module(); sid = str(uuid.uuid4()); base = events(sid)
    rows = [base[0], {'type': 'error', 'session_id': sid, 'message': 'HTTP 500 Internal Server Error'},
            {**base[-1], 'subtype': 'error_during_execution', 'is_error': True,
             'stop_reason': 'refusal' if refused else 'end_turn'}]
    with pytest.raises(m.agy.RuntimeError) as error: parse(rows, sid)
    assert isinstance(error.value, m.agy.ProviderUnavailable) is not refused


@pytest.mark.parametrize('message', ['HTTP 500 Internal Server Error', 'HTTP 429 Too Many Requests',
                                   'HTTP 401 Unauthorized', 'HTTP 402 Payment Required',
                                   'Image generation is a SuperGrok feature and is not available on Free or X Basic'])
def test_service_errors_are_failover_eligible(message):
    m = module()
    with pytest.raises(m.agy.ProviderUnavailable): m.provider_error(message)


@pytest.mark.parametrize('message', ['HTTP 403 Forbidden', 'content safety refusal', 'invalid image',
                                   'policy refusal: HTTP 500', 'generation refused: HTTP 500',
                                   'content blocked: HTTP 500', 'prompt prohibited: HTTP 500',
                                   'content_filter: HTTP 500', 'invalid prompt: HTTP 500'])
def test_policy_and_unknown_errors_do_not_fail_over(message):
    m = module()
    with pytest.raises(m.agy.RuntimeError) as error: m.provider_error(message)
    assert not isinstance(error.value, m.agy.ProviderUnavailable)


def test_artifact_rejects_reference_copy_multiple_files_and_symlink(tmp_path):
    m = module(); sid = str(uuid.uuid4())
    d = tmp_path/'.grok/sessions/%2Fwork'/sid/'images'; d.mkdir(parents=True)
    (d/'1.jpg').write_bytes(png())
    assert m.artifact(tmp_path, sid, []) == png()
    with pytest.raises(m.agy.RuntimeError): m.artifact(tmp_path, sid, [png()])
    (d/'2.jpg').write_bytes(png('blue'))
    with pytest.raises(m.agy.RuntimeError): m.artifact(tmp_path, sid, [])
    (d/'2.jpg').unlink(); (d/'1.jpg').unlink()
    outside=tmp_path/'private.png'; outside.write_bytes(png()); (d/'1.jpg').symlink_to(outside)
    with pytest.raises(m.agy.RuntimeError): m.artifact(tmp_path, sid, [])


def test_non_owner_commander_is_rejected_before_runtime():
    m=module()
    with pytest.raises(m.agy.RuntimeError): m.render({'user_id': 'other', 'include_commander': True}, {})


def test_environment_drops_secrets_and_provider_overrides():
    env=module().environment({'HOME': '/home/test', 'XAI_API_KEY': 'secret',
        'GROK_CONFIG': 'unsafe', 'GROK_AUTH_PROVIDER_COMMAND': 'unsafe', 'DBUS_SESSION_BUS_ADDRESS': 'unsafe'})
    assert not any(k in env for k in ['XAI_API_KEY','GROK_CONFIG','GROK_AUTH_PROVIDER_COMMAND','DBUS_SESSION_BUS_ADDRESS'])


def test_sparse_sandbox_cannot_read_owner_config_or_modify_auth(tmp_path):
    m=module(); profile,work=m.prepare(tmp_path, {'/work/klukai.png'})
    auth=tmp_path/'auth'; auth.write_text('test credential fixture')
    binary=Path('/usr/bin/true')
    code='''import os,pathlib
h=pathlib.Path.home()
assert (h/'.grok/auth.json').read_text()=='test credential fixture'
assert not (h/'.ssh').exists()
assert not pathlib.Path('/run/user').exists()
assert not pathlib.Path('/etc/grok').exists()
try: (h/'.grok/auth.json').write_text('corrupt')
except OSError: pass
else: raise AssertionError('auth writable')
try: (h/'.grok/config.toml').write_text('corrupt')
except OSError: pass
else: raise AssertionError('config writable')
print('isolated')'''
    cmd=m.command(profile,work,[],auth=auth,binary=binary)
    cmd=cmd[:-1]+['/usr/bin/python3','-c',code]
    run=subprocess.run(cmd,env=m.environment(),capture_output=True,text=True,timeout=10)
    assert run.returncode == 0,run.stderr
    assert run.stdout.strip()=='isolated'
    assert auth.read_text()=='test credential fixture'


def test_pinned_copy_is_preferred_and_downloads_are_the_fallback(tmp_path, monkeypatch):
    m=module()
    home=tmp_path/'home'; pinned=tmp_path/'pinned'; downloads=home/'.grok/downloads'
    for d in (pinned, downloads): d.mkdir(parents=True)
    auth=home/'.grok/auth.json'; auth.write_text('{}')
    monkeypatch.setattr(m.Path, 'home', lambda: home)
    monkeypatch.setattr(m, 'PINNED_DIR', pinned)
    name=f'grok-{m.VERSION}-linux-x86_64'
    profile=tmp_path/'p'; profile.mkdir(); work=tmp_path/'w'; work.mkdir()
    with pytest.raises(m.agy.ProviderUnavailable):
        m.command(profile, work, [])
    (downloads/name).write_text('')
    assert str(downloads/name) in m.command(profile, work, [])
    (pinned/name).write_text('')
    cmd=m.command(profile, work, [])
    assert str(pinned/name) in cmd and str(downloads/name) not in cmd
