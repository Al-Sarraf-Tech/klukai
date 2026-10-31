"""Grok's bounded worker must not upload unapproved files or repeat edits."""
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
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


def test_cancel_stops_the_cli_without_another_attempt(tmp_path, monkeypatch):
    m = module()
    auth = tmp_path / "auth.json"
    auth.write_text("login")
    monkeypatch.setenv("GROK_AUTH_PATH", str(auth))
    monkeypatch.setattr(m, "command", lambda *args, **kwargs: ["/bin/sleep", "30"])
    cancel = threading.Event()

    def trip():
        time.sleep(0.15)
        cancel.set()

    threading.Thread(target=trip).start()
    with pytest.raises(m.agy.RuntimeError, match="cancelled"):
        m.execute(tmp_path, tmp_path, ["ignored"], timeout=5, cancel=cancel)


def test_portrait_frame_uses_the_nearest_supported_aspect_ratio():
    assert module().aspect_ratio(832, 1216) == "2:3"
    assert module().aspect_ratio(1216, 832) == "3:2"
    assert module().aspect_ratio(1024, 1024) == "1:1"


def test_gate_requires_the_requested_aspect_ratio_when_the_policy_sets_one(tmp_path):
    gate = module("grok_image_gate")
    policy = {"images": ["/work/klukai.png"], "aspect_ratio": "2:3"}
    call = {"toolName": "image_edit", "toolInput": {
        "image": ["/work/klukai.png"], "prompt": "Draw", "aspect_ratio": "2:3",
    }}
    assert gate.decide(call, policy, tmp_path / "claim") == "allow"
    call["toolInput"]["aspect_ratio"] = "1:1"
    assert gate.decide(call, policy, tmp_path / "square") == "deny"
    del call["toolInput"]["aspect_ratio"]
    assert gate.decide(call, policy, tmp_path / "missing") == "deny"


def test_bundled_skills_are_accepted_and_anything_else_is_rejected(tmp_path):
    m = module()
    sid = str(uuid.uuid4())
    root = tmp_path / "profile" / ".grok" / "bundled" / "skills"
    (root / "imagine").mkdir(parents=True)
    (root / "build-with-ai").mkdir()
    (root / "code-review").mkdir()
    allowed = m.bundled_skills(tmp_path / "profile")
    rows = events(sid)
    rows[0]["skills"] = ["build-with-ai", "bundled:imagine", {"name": "code-review"}]
    assert m.parse_stream(
        "\n".join(map(json.dumps, rows)), sid, {"/work/klukai.png"}, allowed_skills=allowed,
    ) is None
    rows[0]["skills"] = ["build-with-ai", "user-added"]
    with pytest.raises(m.agy.RuntimeError, match="bundled"):
        m.parse_stream("\n".join(map(json.dumps, rows)), sid, {"/work/klukai.png"}, allowed_skills=allowed)
    rows[0].pop("skills")
    with pytest.raises(m.agy.RuntimeError, match="missing"):
        m.parse_stream("\n".join(map(json.dumps, rows)), sid, {"/work/klukai.png"}, allowed_skills=allowed)
    rows = events(sid)
    rows[1]["message"]["content"][0]["input"]["aspect_ratio"] = "2:3"
    assert m.parse_stream(
        "\n".join(map(json.dumps, rows)), sid, {"/work/klukai.png"}, aspect="2:3",
    ) is None
    del rows[1]["message"]["content"][0]["input"]["aspect_ratio"]
    with pytest.raises(m.agy.RuntimeError, match="unapproved"):
        m.parse_stream("\n".join(map(json.dumps, rows)), sid, {"/work/klukai.png"}, aspect="2:3")


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
                                   'Image generation is a SuperGrok feature and is not available on Free or X Basic',
                                   # The CLI's own words when its account is signed out (2026-10-21).
                                   'Error: Not signed in. To authenticate without a browser, run:\n'
                                   '  grok login --device-code\nAlternatively, set the XAI_API_KEY '
                                   'environment variable or run `grok login` on a machine with a browser.\n'
                                   '{"type":"result","subtype":"error_during_execution","is_error":true,'
                                   '"num_turns":0,"stop_reason":null}'])
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


def test_runs_unsandboxed_in_its_own_home_with_the_real_login(tmp_path):
    """Owner directive 2026-10-21: no sandbox. The CLI runs directly with a
    clean home of its own and the account's real login, which it can renew."""
    m=module(); profile,work=m.prepare(tmp_path, {str(tmp_path/'work'/'klukai.png')})
    auth=tmp_path/'auth.json'; auth.write_text('test credential fixture')
    binary=Path('/usr/bin/true')
    assert m.command(profile,work,['--single','x'],auth=auth,binary=binary) == ['/usr/bin/true','--single','x']
    env=m.environment({'HOME': '/home/owner', 'XAI_API_KEY': 'secret'}, home=profile, auth=auth)
    assert env['HOME']==str(profile) and env['GROK_HOME']==str(profile/'.grok')
    assert env['GROK_AUTH_PATH']==str(auth) and 'XAI_API_KEY' not in env
    code="""import os,pathlib
a=pathlib.Path(os.environ['GROK_AUTH_PATH'])
assert a.read_text()=='test credential fixture'
a.write_text('renewed login')
assert pathlib.Path.home()==pathlib.Path(os.environ['HOME'])
print('direct')"""
    run=subprocess.run(['/usr/bin/python3','-c',code],env=env,cwd=work,capture_output=True,text=True,timeout=10)
    assert run.returncode == 0,run.stderr
    assert run.stdout.strip()=='direct'
    assert auth.read_text()=='renewed login'  # a renewal is kept


def test_the_gate_hook_finds_its_policy_and_work_beside_itself(tmp_path):
    m=module(); image=str(tmp_path/'work'/'klukai.png')
    profile,work=m.prepare(tmp_path, {image})
    hook=json.loads((profile/'.grok/hooks/image-gate.json').read_text())
    command=hook['hooks']['PreToolUse'][0]['hooks'][0]['command']
    assert command == '/usr/bin/python3 ' + str(tmp_path/'gate'/'gate.py')
    call={'toolName':'image_edit','toolInput':{'image':[image],'prompt':'Draw'},'sessionId':'s','toolUseId':'t'}
    run=subprocess.run(['/usr/bin/python3',str(tmp_path/'gate'/'gate.py')],input=json.dumps(call),
                       capture_output=True,text=True,timeout=10)
    assert run.returncode == 0 and json.loads(run.stdout)['decision']=='allow'
    assert (work/'generation-started').exists()
    assert json.loads((work/'gate-audit.ndjson').read_text())['decision']=='allow'


def _ready(monkeypatch, tmp_path, module):
    monkeypatch.setenv("KLUKAI_GROK_DAILY_LIMIT", "10")
    monkeypatch.setenv("KLUKAI_GROK_MONTHLY_LIMIT", "150")
    monkeypatch.setenv("KLUKAI_GROK_LEDGER", str(tmp_path / "grok.sqlite3"))
    return module


def test_render_locks_anime_style_uses_text_commander_and_grok_4_7_medium(tmp_path, monkeypatch):
    m = _ready(monkeypatch, tmp_path, module())
    reads = []
    monkeypatch.setattr(m, "read_image", lambda entry: reads.append(entry) or png())
    captured = {}

    def execute(profile, work, args, timeout=170):
        captured["args"] = args
        session = args[args.index("--session-id") + 1]
        (work / "gate-audit.ndjson").write_text(json.dumps({
            "decision": "allow", "tool": "image_edit", "session": session,
        }))
        return "{}"

    monkeypatch.setattr(m, "execute", execute)
    monkeypatch.setattr(m, "parse_stream", lambda *a, **k: None)
    monkeypatch.setattr(m, "artifact", lambda *a, **k: png("blue"))
    request = {"request_id": "r", "user_id": "jalsarraf", "prompt": "rooftop garden",
               "width": 832, "height": 1216, "include_commander": True}
    assert m.render(request, {"klukai": "KLUkai-REF", "commander": "COMMANDER-REF"}).startswith(b"\x89PNG")
    assert reads == ["KLUkai-REF"]
    prompt = captured["args"][-1]
    assert prompt.startswith("Create exactly one polished illustration")
    assert "2D anime illustration" in prompt and "NOT photorealistic" in prompt
    assert "hand-painted anime background art" in prompt
    assert "text-only appearance guidance" in prompt and "Exactly two" in prompt
    assert "rooftop garden" in prompt
    assert "aspect_ratio to 2:3" in prompt
    assert captured["args"][captured["args"].index("--model") + 1] == "grok-4.7"
    assert captured["args"][captured["args"].index("--effort") + 1] == "xhigh"
    assert "disable_api_key_auth = true" in m.CONFIG
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(tmp_path / "grok.sqlite3")) as db:
        assert db.execute("SELECT COUNT(*) FROM grok_attempts").fetchone()[0] == 1


def test_nudity_and_missing_caps_fail_before_references_or_the_ledger(tmp_path, monkeypatch):
    m = module()
    monkeypatch.delenv("KLUKAI_GROK_DAILY_LIMIT", raising=False)
    monkeypatch.delenv("KLUKAI_GROK_MONTHLY_LIMIT", raising=False)
    monkeypatch.setenv("KLUKAI_GROK_LEDGER", str(tmp_path / "grok.sqlite3"))
    reads = []
    monkeypatch.setattr(m, "read_image", lambda entry: reads.append(entry) or png())
    monkeypatch.setattr(m, "execute", lambda *a, **k: (_ for _ in ()).throw(AssertionError("cli")))
    request = {"request_id": "r", "user_id": "jalsarraf", "prompt": "scene",
               "width": 832, "height": 1216, "include_commander": False, "nudity": True}
    with pytest.raises(m.agy.RuntimeError, match="Nudity rendering is unsupported"):
        m.render(request, {"klukai": "K", "commander": "C"})
    request.pop("nudity")
    with pytest.raises(m.agy.ProviderUnavailable, match="allowance not configured"):
        m.render(request, {"klukai": "K", "commander": "C"})
    assert reads == []
    assert not (tmp_path / "grok.sqlite3").exists()


def test_stream_rejects_a_session_that_is_not_grok_4_7_at_xhigh():
    m = module()
    sid = str(uuid.uuid4())
    rows = events(sid)
    rows[0]["model"] = "grok-4.6"
    with pytest.raises(m.agy.RuntimeError, match="not grok-4.7"):
        parse(rows, sid)
    rows = events(sid)
    rows[0]["reasoning_effort"] = "high"
    with pytest.raises(m.agy.RuntimeError, match="not xhigh"):
        parse(rows, sid)


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
