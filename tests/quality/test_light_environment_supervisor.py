"""Parent lifetime and normal bwrap completion are part of command ownership."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
FINAL_REAP = r"""
import ctypes,importlib.util,json,os,pathlib,signal,subprocess,sys,time
base=pathlib.Path(__file__).parent;root=globals().get('root',sys.argv[2]);mode=globals().get('mode',sys.argv[3])
def identity(pid):
    fields=pathlib.Path(f'/proc/{pid}/stat').read_text().rpartition(') ')[2].split()
    return int(fields[1]),int(fields[19])
def until(predicate):
    end=time.monotonic()+2
    while time.monotonic()<end:
        if predicate(): return
        time.sleep(.002)
    raise TimeoutError('owned fixture gate expired')
def helper():
    spec=importlib.util.spec_from_file_location('supervisor',pathlib.Path(root)/'tools/ci/light_environment_supervisor.py')
    supervisor=importlib.util.module_from_spec(spec);spec.loader.exec_module(supervisor)
    actual=supervisor.reap;first=True
    def gated(record,child):
        nonlocal first
        if first:
            first=False
            until(lambda:(base/'ready').exists())
            (base/'gate').write_text(str(child.pid))
            until(lambda:(base/'release').exists())
            until(lambda:(base/'done').exists())
            time.sleep(.04)  # Schedule only; all real wait/status results remain intact.
        return actual(record,child)
    supervisor.reap=gated;supervisor.main()
def caller():
    sys.path.insert(0,root)
    from tools.ci import light_environment_supervisor as supervisor
    from tools.ci.light_environment_process import Commands
    supervisor.__file__=str(base/'helper.py')
    try: Commands(base/'logs',2).run([sys.executable,'-B',str(__file__),'command',root,mode])
    except ValueError: pass
def command():
    (base/'ready').write_text(str(os.getpid()))
    until(lambda:(base/'release').exists())
    (base/'done').write_text('natural exit 0')
def outer():
    if ctypes.CDLL(None,use_errno=True).prctl(36,1,0,0,0): raise OSError('outer subreaper unavailable')
    (base/'logs').mkdir()
    worker=subprocess.Popen([sys.executable,'-B',str(__file__),'caller',root,mode])
    descriptor=os.pidfd_open(worker.pid);reaped=[];rescue=False;result={}
    try:
        until(lambda:(base/'gate').exists())
        command_pid=int((base/'gate').read_text());command_before=identity(command_pid)
        worker_before=identity(worker.pid)
        if worker_before[0]!=os.getpid() or identity(worker.pid)!=worker_before: raise RuntimeError('caller identity changed')
        if mode=='parent-death':
            signal.pidfd_send_signal(descriptor,signal.SIGKILL);worker.wait(timeout=.5)
        elif mode=='cancel-file':
            (base/'logs/command-000.supervisor.cancel').write_text('stop before final reap')
        else:
            supervisor_pid=command_before[0];before=identity(supervisor_pid)
            fd=os.pidfd_open(supervisor_pid)
            try:
                if before[0]!=worker.pid or identity(supervisor_pid)!=before: raise RuntimeError('supervisor identity changed')
                signal.pidfd_send_signal(fd,signal.SIGTERM)
            finally: os.close(fd)
        (base/'release').write_text('boundary event occurred while command was active')
        until(lambda:worker.poll() is not None)
        end=time.monotonic()+2
        while time.monotonic()<end:
            try: pid,status=os.waitpid(-1,os.WNOHANG)
            except ChildProcessError: break
            if pid: reaped.append({'pid':pid,'exit_code':os.waitstatus_to_exitcode(status)})
            time.sleep(.002)
        else: raise TimeoutError('supervisor not reaped')
        result={'command_before_event':command_before,'supervisor':json.loads((base/'logs/command-000.supervisor.json').read_text())}
    finally:
        os.close(descriptor)
        end=time.monotonic()+2
        while time.monotonic()<end:
            try: pid,status=os.waitpid(-1,os.WNOHANG)
            except ChildProcessError: break
            if pid:
                reaped.append({'pid':pid,'exit_code':os.waitstatus_to_exitcode(status)})
                if pid==worker.pid: worker.returncode=os.waitstatus_to_exitcode(status)
                continue
            for raw in pathlib.Path(f'/proc/self/task/{os.getpid()}/children').read_text().split():
                owned=int(raw);fd=None
                try:
                    before=identity(owned);fd=os.pidfd_open(owned)
                    if before[0]==os.getpid() and identity(owned)==before:
                        rescue=True;signal.pidfd_send_signal(fd,signal.SIGKILL)
                except (FileNotFoundError,ProcessLookupError): pass
                finally:
                    if fd is not None: os.close(fd)
            time.sleep(.005)
        else: raise RuntimeError('outer cleanup incomplete')
        result.update(rescue=rescue,outer_reaped=reaped)
        (base/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
if __name__=='__main__':
    {'outer':outer,'caller':caller,'command':command,'helper':helper}[sys.argv[1]]()
"""
PARENT_DEATH = r'''
import ctypes,json,os,pathlib,signal,subprocess,sys,time
base=pathlib.Path(sys.argv[1]);root=sys.argv[2];early=sys.argv[3]=='early'
libc=ctypes.CDLL(None,use_errno=True)
if libc.prctl(36,1,0,0,0): raise OSError('fixture subreaper unavailable')
worker_code=r"""
import os,pathlib,subprocess,sys
sys.path.insert(0,sys.argv[2])
from tools.ci.light_environment_process import Commands
out=pathlib.Path(sys.argv[1]);early=sys.argv[3]=='early'
if early:
    actual=subprocess.Popen
    gate="import pathlib,runpy,sys,time; p=pathlib.Path(sys.argv[1]); (p/'ready').write_text('ready'); deadline=time.monotonic()+3\nwhile not (p/'release').exists() and time.monotonic()<deadline: time.sleep(.005)\nsys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name='__main__')"
    def delayed(argv,**kwargs):
        return actual([sys.executable,'-I','-c',gate,str(out),*argv[2:]],**kwargs)
    subprocess.Popen=delayed
code="import os,pathlib,sys,time\nif os.fork()==0:\n os.setsid(); pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30); os._exit(0)\ntime.sleep(30)"
Commands(out,30).run([sys.executable,'-I','-c',code,str(out/'active')])
"""
worker=subprocess.Popen([sys.executable,'-I','-c',worker_code,str(base),root,sys.argv[3]])
worker_fd=os.pidfd_open(worker.pid)
end=time.monotonic()+3
marker=base/('ready' if early else 'active')
while not marker.exists() and worker.poll() is None and time.monotonic()<end: time.sleep(.005)
ready=marker.exists()
signal.pidfd_send_signal(worker_fd,signal.SIGKILL);os.close(worker_fd)
worker.wait(timeout=1)
started=time.monotonic()
if early: (base/'release').write_text('release')
reaped=[];rescue=False
for cleanup in (False,True):
    end=time.monotonic()+(2 if cleanup else .7)
    while time.monotonic()<end:
        try: pid,status=os.waitpid(-1,os.WNOHANG)
        except ChildProcessError: break
        if pid: reaped.append({'pid':pid,'exit_code':os.waitstatus_to_exitcode(status)})
        if cleanup:
            rescue=True
            for raw in pathlib.Path(f'/proc/self/task/{os.getpid()}/children').read_text().split():
                pid=int(raw);fd=None
                try:
                    def identity():
                        f=pathlib.Path(f'/proc/{pid}/stat').read_text().rpartition(') ')[2].split()
                        return int(f[1]),int(f[19])
                    before=identity();fd=os.pidfd_open(pid)
                    if before[0]==os.getpid() and identity()==before: signal.pidfd_send_signal(fd,signal.SIGKILL)
                except (ProcessLookupError,FileNotFoundError): pass
                finally:
                    if fd is not None: os.close(fd)
        time.sleep(.005)
    else:
        if cleanup: raise RuntimeError('fixture cleanup incomplete')
        continue
    break
report=base/'command-000.supervisor.json'
result={'ready':ready,'rescue':rescue,'elapsed':time.monotonic()-started,'reaped':reaped,
        'supervisor':json.loads(report.read_text()) if report.exists() else None,'active':(base/'active').exists()}
(base/'outer-result.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
'''


@pytest.mark.parametrize("event", ["parent-death", "cancel-file", "signal"])
def test_final_successful_reap_cannot_hide_an_active_command_stop(tmp_path, event):
    control = tmp_path / "race_control.py"
    control.write_text(FINAL_REAP)
    # The helper receives the ordinary report/timeout/command argv. Preserve it
    # for supervisor.main while the scheduling wrapper uses explicit fixture data.
    (tmp_path / "helper.py").write_text(
        "import runpy,sys\n"
        f"runpy.run_path({str(control)!r},init_globals={{'root':{str(ROOT)!r},"
        f"'mode':{event!r}}},run_name='fixture')['helper']()\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", str(control), "outer", str(ROOT), event],
        capture_output=True,
        text=True,
        timeout=7,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["rescue"] is False
    state = data["supervisor"]
    assert state["exit_code"] == 0 and state["cleanup_complete"] is True
    assert state["terminated"] == []
    assert state["passed"] is False
    assert all(row["exit_code"] == 0 for row in state["reaped"])
    if event == "parent-death":
        assert state["parent_dead"] is True
    elif event == "cancel-file":
        assert state["parent_stop"] is True
    else:
        assert state["signals"] == [15]


@pytest.mark.parametrize("phase", ["early", "active"])
def test_original_caller_death_fails_closed_and_reaps_without_outer_rescue(
    tmp_path, phase
):
    result = subprocess.run(
        [sys.executable, "-I", "-c", PARENT_DEATH, str(tmp_path), str(ROOT), phase],
        capture_output=True,
        text=True,
        timeout=7,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["ready"] is True
    assert data["rescue"] is False
    assert data["elapsed"] < 0.7
    state = data["supervisor"]
    assert state["passed"] is False and state["cleanup_complete"] is True
    assert len(data["reaped"]) == 1  # Outer reaps only the now-orphaned supervisor.
    if phase == "early":
        assert data["active"] is False
        assert "command_pid" not in state
    else:
        assert state["parent_dead"] is True
        assert len(state["reaped"]) == 2


def test_actual_bwrap_normal_exit_is_reaped_without_orphan_failure(tmp_path):
    from tools.ci import light_environment_sandbox as sandbox
    from tools.ci.light_environment_process import Commands

    for name in ("builder", "downloads", "wheels", "logs"):
        (tmp_path / name).mkdir()
    lock = tmp_path / "lock"
    lock.write_text("owned offline fixture")
    commands = Commands(tmp_path / "logs", 1)
    argv = [
        *sandbox.command(
            tmp_path / "builder", tmp_path / "downloads", tmp_path / "wheels", lock
        ),
        "--",
        "/usr/bin/true",
    ]
    commands.run(argv)
    state = commands.records[0]["supervisor"]
    assert state["passed"] is True and state["cleanup_complete"] is True
    assert all(row["exit_code"] == 0 for row in state["reaped"])
    assert state["terminated"] == []


def test_parent_pidfd_acquisition_failure_fails_before_command_spawn(tmp_path):
    from tools.ci import light_environment_supervisor as supervisor

    code = """import os,pathlib,sys
sys.path.insert(0,sys.argv[1])
from tools.ci import light_environment_supervisor as supervisor
actual=os.pidfd_open
def denied(pid,*args):
    if pid==os.getppid(): raise PermissionError('fixture: parent pidfd denied')
    return actual(pid,*args)
os.pidfd_open=denied
raise SystemExit(supervisor.supervise([sys.executable,'-c','raise RuntimeError("must not spawn")'],1,pathlib.Path(sys.argv[2])))
"""
    report = tmp_path / "supervisor.json"
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(ROOT), str(report)],
        capture_output=True,
        text=True,
        timeout=3,
        env={
            **os.environ,
            supervisor.PARENT_VARIABLE: (
                f"{os.getpid()}:{supervisor.identity(os.getpid())[1]}"
            ),
        },
    )
    assert result.returncode == 1, result.stderr
    state = json.loads(report.read_text())
    assert state["error_type"] == "PermissionError"
    assert state["capabilities"] == {"pidfd": True, "subreaper": True}
    assert state["passed"] is False and state["cleanup_complete"] is True
    assert "command_pid" not in state


def test_parent_identity_change_after_pidfd_acquisition_closes_descriptor(monkeypatch):
    from tools.ci import light_environment_supervisor as supervisor

    pid = os.getpid()
    monkeypatch.setenv(
        supervisor.PARENT_VARIABLE, f"{pid}:{supervisor.identity(pid)[1]}"
    )
    parents = iter([pid, pid + 1])
    monkeypatch.setattr(supervisor.os, "getppid", lambda: next(parents))
    actual = os.pidfd_open
    opened = []

    def capture(target):
        descriptor = actual(target)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(supervisor.os, "pidfd_open", capture)
    with pytest.raises(RuntimeError, match="caller changed"):
        supervisor.parent_handle({})
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])
