"""Contract checks for the restricted account-authenticated Agy process."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / 'scripts/agy_art_runtime.py'

def runtime():
    spec = importlib.util.spec_from_file_location('agy_art_runtime', SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def stream(tools=None, status='SUCCESS', read=True):
    rows = [{'event':'init','conversation_id':'test','init':{'tools':tools or ['view_file'], 'model':'gemini-3.8-flash-medium','agent':'klukai-visual-review'}}]
    if read:
        rows.append({'event':'step_update','step_update':{'conversation_id':'test','step_index':2,'step_type':'tool','state':'DONE','tool_name':'view_file','tool_info':{'name':'view_file','parameters':{'AbsolutePath':'/work/0.png'},'output':'Viewed image'}}})
    rows.append({'event':'result','result':{'conversation_id':'test','status':status,'structured_output':{'ok':True}}})
    return '\n'.join(map(json.dumps, rows))

def test_environment_keeps_keyring_session_but_excludes_paid_auth():
    env=runtime().environment({'HOME':'/owner','PATH':'/evil','DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/1000/bus','AGY_ADC_AUTH':'true','GEMINI_API_KEY':'secret','GOOGLE_APPLICATION_CREDENTIALS':'/private'})
    assert env['HOME']=='/owner'
    assert 'DBUS_SESSION_BUS_ADDRESS' in env
    assert not any(k in env for k in ['AGY_ADC_AUTH','GEMINI_API_KEY','GOOGLE_APPLICATION_CREDENTIALS'])
    assert env['PATH']=='/usr/bin:/bin'

def test_accepts_actual_image_read_and_structured_success():
    r=runtime().parse_stream(stream(), {'/work/0.png'})
    assert r['structured_output']=={'ok':True}
    assert r['images_read']==['/work/0.png']

@pytest.mark.parametrize('raw', [stream().replace('view_file', 'generate_image'),stream(status='ERROR'),stream(read=False),stream().splitlines()[0]])
def test_rejects_incomplete_or_unexpected_tool_calls(raw):
    m=runtime()
    with pytest.raises(m.RuntimeError): m.parse_stream(raw, {'/work/0.png'})

def test_rejects_denied_action_even_when_terminal_success():
    m=runtime(); rows=[json.loads(x) for x in stream().splitlines()]
    rows[-1]['result']['denied_actions']=['read_file(/private)']
    with pytest.raises(m.RuntimeError): m.parse_stream('\n'.join(map(json.dumps,rows)), {'/work/0.png'})

def test_watchdog_kills_hung_process_and_does_not_retry(tmp_path, monkeypatch):
    import sys
    m=runtime(); marker=tmp_path/'calls'
    script=tmp_path/'hung.py'
    script.write_text('from pathlib import Path\nimport time\np=Path('+repr(str(marker))+')\np.write_text(p.read_text()+"x" if p.exists() else "x")\ntime.sleep(60)\n')
    monkeypatch.setattr(m,'command',lambda *a:[sys.executable,str(script)])
    with pytest.raises(m.RuntimeError,match='timed out'):
        m.execute(tmp_path,tmp_path,[],0.2)
    assert marker.read_text()=='x'

def test_nonzero_cli_cannot_leak_raw_error_into_report(tmp_path, monkeypatch):
    import sys
    m=runtime()
    monkeypatch.setattr(m,'command',lambda *a:[sys.executable,'-c','import sys;print("private data",file=sys.stderr);sys.exit(9)'])
    with pytest.raises(m.RuntimeError) as e: m.execute(tmp_path,tmp_path,[],5)
    assert 'private data' not in str(e.value)
    assert '9' in str(e.value)

def test_profile_disables_credit_fallback_and_global_tools(tmp_path):
    m=runtime();profile,work=m.prepare(tmp_path)
    settings=json.loads((profile/'.gemini/antigravity-cli/settings.json').read_text())
    assert settings['useG1Credits'] is False
    assert not settings.get('modelProvider')
    assert 'command(*)' in settings['permissions']['deny']
    assert 'mcp(*)' in settings['permissions']['deny']
    assert not (profile/'.gemini/GEMINI.md').exists()
    assert settings['permissions']['allow']==[f'read_file({work})']
    assert settings['trustedWorkspaces']==[str(work)]

def test_gate_blocks_generation_commands_and_unselected_files():
    spec=importlib.util.spec_from_file_location('agy_art_gate', SCRIPT.with_name('agy_art_gate.py'))
    g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
    p={'images':['/work/0.png']}
    for name in ['generate_image','run_command','invoke_subagent','view_file']:
        assert g.decide({'name':name,'args':{'AbsolutePath':'/etc/passwd'}},p)=='deny'
    assert g.decide({'name':'view_file','args':{'AbsolutePath':'/work/0.png'}},p)=='allow'
    assert g.decide({'name':'finish'},p)=='allow'

def test_failed_finish_cannot_hide_behind_success_envelope():
    m=runtime();rows=[json.loads(x) for x in stream().splitlines()]
    rows.insert(-1,{'event':'step_update','step_update':{'conversation_id':'test','step_index':4,
        'step_type':'tool','state':'ERROR','tool_name':'finish','tool_info':{'error':{'type':'failed'}}}})
    with pytest.raises(m.RuntimeError):m.parse_stream('\n'.join(map(json.dumps,rows)),{'/work/0.png'})

def test_later_unfinished_read_is_not_satisfied_by_prior_completed_read():
    m=runtime();rows=[json.loads(x) for x in stream().splitlines()]
    import copy
    step=copy.deepcopy(rows[1]);step['step_update'].update(step_index=5,state='ACTIVE')
    rows.insert(-1,step)
    with pytest.raises(m.RuntimeError):m.parse_stream('\n'.join(map(json.dumps,rows)),{'/work/0.png'})

def test_result_must_be_final_event():
    m=runtime();rows=[json.loads(x) for x in stream().splitlines()]
    rows.append(rows[1])
    with pytest.raises(m.RuntimeError):m.parse_stream('\n'.join(map(json.dumps,rows)),{'/work/0.png'})

def test_real_active_to_done_read_and_terminal_finish_lifecycle():
    m=runtime();rows=[json.loads(x) for x in stream().splitlines()]
    import copy
    active=copy.deepcopy(rows[1]);active['step_update']['state']='ACTIVE';rows.insert(1,active)
    # Agy 1.3.5 emits ACTIVE finish followed directly by result SUCCESS.
    rows.insert(-1,{'event':'step_update','step_update':{'conversation_id':'test','step_index':4,
        'step_type':'tool','state':'ACTIVE','tool_name':'finish','tool_info':{'name':'finish'}}})
    assert m.parse_stream('\n'.join(map(json.dumps,rows)),{'/work/0.png'})['images_read']==['/work/0.png']

def test_native_finish_error_is_rejected():
    m=runtime();rows=[json.loads(x) for x in stream().splitlines()]
    rows.insert(-1,{'event':'step_update','step_update':{'conversation_id':'test','step_index':4,
        'step_type':'finish','state':'ERROR','tool_info':{'error':{'type':'failed'}}}})
    with pytest.raises(m.RuntimeError):m.parse_stream('\n'.join(map(json.dumps,rows)),{'/work/0.png'})

def test_native_finish_done_is_valid():
    m=runtime();rows=[json.loads(x) for x in stream().splitlines()]
    rows.insert(-1,{'event':'step_update','step_update':{'conversation_id':'test','step_index':4,
        'step_type':'finish','state':'DONE'}})
    assert m.parse_stream('\n'.join(map(json.dumps,rows)),{'/work/0.png'})['status']=='SUCCESS'

def generation_stream(state='DONE', count=1):
    rows=[json.loads(x) for x in stream().splitlines()]
    rows[-1]['result']['structured_output']=None
    for i in range(count):
        rows.insert(-1,{'event':'step_update','step_update':{'conversation_id':'test',
            'step_index':3+i,'step_type':'tool','state':state,'tool_name':'generate_image',
            'tool_info':{'parameters':{'ImagePaths':['/work/0.png']}}}})
    return '\n'.join(map(json.dumps,rows))

def test_generation_requires_one_completed_call_and_explicit_mode():
    m=runtime()
    assert m.parse_stream(generation_stream(),{'/work/0.png'},generation=True)['status']=='SUCCESS'
    for raw in [generation_stream('ACTIVE'),generation_stream(count=0),generation_stream(count=2)]:
        with pytest.raises(m.RuntimeError):m.parse_stream(raw,{'/work/0.png'},generation=True)
    with pytest.raises(m.RuntimeError):m.parse_stream(generation_stream(),{'/work/0.png'})

def test_generation_gate_requires_exact_references_and_only_one_call(tmp_path):
    spec=importlib.util.spec_from_file_location('agy_art_gate', SCRIPT.with_name('agy_art_gate.py'))
    g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
    policy={'images':['/work/0.png'],'generation':True}
    call={'name':'generate_image','args':{'ImagePaths':['/work/0.png']}}
    assert g.decide(call,{'images':['/work/0.png']},tmp_path/'claim')=='deny'
    assert g.decide({'name':'generate_image','args':{'ImagePaths':['/secret']}},policy,tmp_path/'claim')=='deny'
    assert g.decide(call,policy,tmp_path/'claim')=='allow'
    assert g.decide(call,policy,tmp_path/'claim')=='deny'

def test_generation_service_error_permits_bounded_other_provider_failover():
    m=runtime();rows=[json.loads(x) for x in generation_stream().splitlines()]
    step=rows[-2]['step_update'];step['state']='ERROR'
    step['tool_info']['error']={'type':'TOOL_ERROR','message':'failed to generate content: 500 Internal Server Error'}
    with pytest.raises(m.ProviderUnavailable):
        m.parse_stream('\n'.join(map(json.dumps,rows)),{'/work/0.png'},generation=True)

@pytest.mark.parametrize('message', ['403 Forbidden','safety policy refusal: 500','invalid image input','content blocked'])
def test_policy_and_unclassified_tool_errors_do_not_permit_failover(message):
    m=runtime();rows=[json.loads(x) for x in generation_stream().splitlines()]
    step=rows[-2]['step_update'];step['state']='ERROR';step['tool_info']['error']={'message':message}
    with pytest.raises(m.RuntimeError) as error:
        m.parse_stream('\n'.join(map(json.dumps,rows)),{'/work/0.png'},generation=True)
    assert not isinstance(error.value,m.ProviderUnavailable)

@pytest.mark.parametrize('message', ['invalid image dimensions: width must be 500 pixels', 'render this in 500 steps', 'policy refused: failed to generate content: 500 Internal Server Error'])
def test_plain_numbers_or_refusals_are_not_service_statuses(message):
    assert not runtime().service_unavailable({'message':message})

def installed(tmp_path, monkeypatch):
    m=runtime();monkeypatch.setattr(m.Path,'home',lambda:tmp_path)
    binary=tmp_path/'.local/opt/antigravity-cli'/m.VERSION/'agy'
    binary.parent.mkdir(parents=True);binary.write_text('')
    bus=tmp_path/'bus';bus.write_text('')
    return m,binary,'unix:path='+str(bus)

def test_missing_native_account_runtime_is_failover_eligible(tmp_path, monkeypatch):
    m=runtime();monkeypatch.setattr(m.Path,'home',lambda:tmp_path)
    with pytest.raises(m.ProviderUnavailable):m.command(tmp_path,tmp_path,[],'unix:path='+str(tmp_path/'bus'))

def test_missing_keyring_session_is_failover_eligible(tmp_path, monkeypatch):
    m,_,_=installed(tmp_path, monkeypatch)
    with pytest.raises(m.ProviderUnavailable):m.command(tmp_path,tmp_path,[],'unix:path='+str(tmp_path/'gone'))

def test_runs_unsandboxed_in_its_own_home(tmp_path, monkeypatch):
    """Owner directive 2026-10-21: no sandbox. The CLI runs directly, with the
    run's own profile as HOME and the session bus for its keyring login."""
    m,binary,bus=installed(tmp_path, monkeypatch)
    (tmp_path/'run').mkdir();profile,work=m.prepare(tmp_path/'run')
    assert m.command(profile,work,['--print','x'],bus)==[str(binary),'--print','x']
    env=m.environment({'HOME':'/home/owner','DBUS_SESSION_BUS_ADDRESS':bus,'GEMINI_API_KEY':'secret'},home=profile)
    assert env['HOME']==str(profile) and env['DBUS_SESSION_BUS_ADDRESS']==bus
    assert 'GEMINI_API_KEY' not in env
    seen={}
    class Done:
        pid=0;returncode=0
        def wait(self,timeout=None):return 0
    def popen(cmd,**kw):
        seen.update(kw,cmd=cmd);kw['stdout'].write(b'ok');return Done()
    monkeypatch.setattr(m.subprocess,'Popen',popen)
    monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS',bus)
    assert m.execute(profile,work,['--print','x'],5)=='ok'
    assert seen['cmd'][0]==str(binary) and 'bwrap' not in ' '.join(seen['cmd'])
    assert seen['cwd']==work and seen['env']['HOME']==str(profile)

def test_the_gate_hook_finds_its_policy_and_work_beside_itself(tmp_path):
    import subprocess
    m=runtime();profile,work=m.prepare(tmp_path,generation=True)
    image=str(work/'klukai.png')
    m.configure_gate(work,{image},generation=True)
    hooks=json.loads((work/'.agents/hooks.json').read_text())
    command=hooks['klukai-art-gate']['PreToolUse'][0]['hooks'][0]['command']
    assert command=='/usr/bin/python3 '+str(work/'.agents/gate.py')
    for call in ({'name':'view_file','args':{'AbsolutePath':image}},
                 {'name':'generate_image','args':{'ImagePaths':[image]}}):
        run=subprocess.run(['/usr/bin/python3',str(work/'.agents/gate.py')],
                           input=json.dumps({'toolCall':call,'stepIdx':1}),capture_output=True,text=True,timeout=10)
        assert run.returncode==0 and json.loads(run.stdout)['decision']=='allow'
    assert (work/'generation-started').exists()
    m.verify_gate(work,{image},generation=True)

def test_later_refusal_prevents_failover_from_earlier_server_error():
    m=runtime();rows=[json.loads(x) for x in generation_stream().splitlines()]
    step=rows[-2]['step_update'];step['state']='ERROR';step['tool_info']['error']={'message':'failed to generate content: 500 Internal Server Error'}
    rows.insert(-1,{'event':'step_update','step_update':{'conversation_id':'test','step_index':7,'step_type':'finish','state':'ERROR','error':{'message':'Safety policy refusal'}}})
    with pytest.raises(m.RuntimeError) as error:
        m.parse_stream('\n'.join(map(json.dumps,rows)),{'/work/0.png'},generation=True)
    assert not isinstance(error.value,m.ProviderUnavailable)
