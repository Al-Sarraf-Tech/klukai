"""Detached descendants must not escape finite, evidence-producing cleanup."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
OUTER = r'''
import ctypes,json,os,pathlib,signal,subprocess,sys,time
base=pathlib.Path(sys.argv[1]); root=sys.argv[2]; cancel=sys.argv[3]=='True'; early_exit=sys.argv[3]=='exit'
libc=ctypes.CDLL(None,use_errno=True)
if libc.prctl(36,1,0,0,0): raise OSError(ctypes.get_errno(),'outer subreaper')
worker_code=r"""
import json,pathlib,sys
sys.path.insert(0,sys.argv[2])
from tools.ci.light_environment import Commands,cancellation_scope
out=pathlib.Path(sys.argv[1])
code='import os,pathlib,sys,time\npid=os.fork()\nif pid==0:\n    os.setsid()\n    pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n    time.sleep(30)\n    os._exit(0)\ntime.sleep(30)\n'
if sys.argv[3]=='True': code=code.rsplit('time.sleep(30)',1)[0]+"time.sleep(.03);os._exit(0)\n"
with cancellation_scope() as latch:
    try: Commands(out,.2).run([sys.executable,'-I','-c',code,str(out/'detached.pid')])
    except (Exception,KeyboardInterrupt) as error:
        (out/'worker-result.json').write_text(json.dumps({'error':type(error).__name__,'signals':latch.signals}))
"""
worker=subprocess.Popen([sys.executable,'-I','-c',worker_code,str(base),root,str(early_exit)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
fd=os.pidfd_open(worker.pid)
started=time.monotonic(); deadline=started+3
while not (base/'detached.pid').exists() and worker.poll() is None and time.monotonic()<deadline:
    time.sleep(.005)
if cancel and worker.poll() is None:
    for signum in (signal.SIGTERM,signal.SIGINT,signal.SIGTERM,signal.SIGINT):
        try: signal.pidfd_send_signal(fd,signum)
        except ProcessLookupError: break
        time.sleep(.001)
try:
    stdout,stderr=worker.communicate(timeout=1.5)
    rescue=False
except subprocess.TimeoutExpired:
    rescue=True
    signal.pidfd_send_signal(fd,signal.SIGKILL)
    # Outer owns every fixture process, including sessions orphaned by worker.
    end=time.monotonic()+2
    while time.monotonic()<end:
        for raw in pathlib.Path(f'/proc/self/task/{os.getpid()}/children').read_text().split():
            pid=int(raw)
            try:
                owned=os.pidfd_open(pid)
                fields=pathlib.Path(f'/proc/{pid}/stat').read_text().rpartition(') ')[2].split()
                if int(fields[1])==os.getpid(): signal.pidfd_send_signal(owned,signal.SIGKILL)
                os.close(owned)
            except ProcessLookupError: pass
        try:
            pid,status=os.waitpid(-1,os.WNOHANG)
        except ChildProcessError: break
        if pid==worker.pid: worker.returncode=os.waitstatus_to_exitcode(status)
        time.sleep(.005)
    stdout,stderr=worker.communicate(timeout=.5)
os.close(fd)
reaped=[]; end=time.monotonic()+1
while time.monotonic()<end:
    try: pid,status=os.waitpid(-1,os.WNOHANG)
    except ChildProcessError: break
    if pid: reaped.append(pid)
    else: time.sleep(.005)
else: raise RuntimeError('Outer still has an owned child')
result={'rescue':rescue,'elapsed':time.monotonic()-started,'worker_exit':worker.returncode,
        'terminal_evidence':(base/'commands.json').exists(),'outer_reaped':reaped,'stdout':stdout,'stderr':stderr}
(base/'outer-result.json').write_text(json.dumps(result))
print(json.dumps(result))
'''


@pytest.mark.parametrize("cancel", [False, True, "exit"])
def test_detached_child_is_terminated_reaped_and_recorded_without_outer_rescue(
    tmp_path, cancel
):
    result = subprocess.run(
        [sys.executable, "-I", "-c", OUTER, str(tmp_path), str(ROOT), str(cancel)],
        capture_output=True,
        text=True,
        timeout=8,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert not data["rescue"], (
        "Cleanup exceeded the outer bound and required fixture rescue"
    )
    assert data["elapsed"] < 1.5
    assert data["terminal_evidence"] is True
    assert data["outer_reaped"] == [], (
        "The command supervisor must reap its own descendants"
    )
    records = json.loads((tmp_path / "commands.json").read_text())
    state = json.loads((tmp_path / records[0]["supervisor_report"]).read_text())
    assert state["cleanup_complete"] is True
    assert state["capabilities"] == {"pidfd": True, "subreaper": True}
    detached = int((tmp_path / "detached.pid").read_text())
    assert detached in [row["pid"] for row in state["reaped"]]
    with pytest.raises(ProcessLookupError):
        os.kill(detached, 0)
    if cancel is True:
        worker = json.loads((tmp_path / "worker-result.json").read_text())
        assert len(worker["signals"]) >= 2


def test_command_spawn_failure_still_writes_terminal_private_evidence(
    tmp_path, monkeypatch
):
    from tools.ci.light_environment_process import Commands

    commands = Commands(tmp_path, 0.2)

    def fail(*_args, **_kwargs):
        raise OSError("fixture: supervisor spawn failed")

    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(OSError):
        commands.run([sys.executable, "-I", "-c", "pass"])
    record = json.loads((tmp_path / "commands.json").read_text())[0]
    assert record["error_type"] == "OSError"
    assert (tmp_path / record["stdout"]).is_file()
    assert (tmp_path / record["stderr"]).is_file()


def test_supervised_command_spawn_failure_is_reaped_and_reported(tmp_path):
    from tools.ci.light_environment_process import Commands

    with pytest.raises(ValueError):
        Commands(tmp_path, 0.2).run(["/nonexistent-klukai-owned-fixture-command"])
    record = json.loads((tmp_path / "commands.json").read_text())[0]
    state = record["supervisor"]
    assert state["error_type"] == "FileNotFoundError"
    assert state["passed"] is False and state["cleanup_complete"] is True
    assert state["reaped"] == []
    with pytest.raises(ProcessLookupError):
        os.kill(record["pid"], 0)


def test_parent_pidfd_acquisition_failure_still_stops_and_reaps_supervisor(tmp_path):
    code = r"""
import json,os,pathlib,sys
sys.path.insert(0,sys.argv[2])
from tools.ci.light_environment_process import Commands
out=pathlib.Path(sys.argv[1]); commands=Commands(out,.2)
actual=os.pidfd_open
os.pidfd_open=lambda *_: (_ for _ in ()).throw(PermissionError('owned fixture: child pidfd denied'))
try: commands.run([sys.executable,'-I','-c','import time;time.sleep(30)'])
except PermissionError: pass
finally: os.pidfd_open=actual
record=json.loads((out/'commands.json').read_text())[0]
try:
    os.waitpid(record['pid'],0)
    needed_outer_reap=True
except ChildProcessError:
    needed_outer_reap=False
print(json.dumps({'needed_outer_reap':needed_outer_reap,'record':record}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(tmp_path), str(ROOT)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["needed_outer_reap"] is False
    assert data["record"]["supervisor"]["cleanup_complete"] is True
    assert data["record"]["exit_code"] is not None
