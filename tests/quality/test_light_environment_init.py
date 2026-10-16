"""A real namespace init settles all children before native command admission."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
import time

import pytest

from test_light_environment import (  # noqa: F401
    arguments,
    boundary,
    repository,
    subject,
)

from tools.ci import light_environment_sandbox as sandbox
from tools.ci.light_environment_process import Commands

ROOT = Path(__file__).resolve().parents[2]


def native_command(
    tmp_path: Path, code: str, timeout: float = 1
) -> tuple[Commands, list[str]]:
    for name in ("builder", "downloads", "wheels", "logs"):
        (tmp_path / name).mkdir()
    lock = tmp_path / "lock"
    lock.write_text("owned offline fixture")
    return Commands(tmp_path / "logs", timeout), [
        *sandbox.command(
            tmp_path / "builder", tmp_path / "downloads", tmp_path / "wheels", lock
        ),
        "--",
        sandbox.init_material()["python"],
        "-I",
        "-B",
        "-c",
        code,
    ]


def test_initial_zero_waits_for_detached_child_zero_and_natural_settlement(tmp_path):
    code = """import os,time
reader,writer=os.pipe()
if os.fork()==0:
    os.close(reader)
    os.setsid()
    os.write(writer,b'ready')
    os.close(writer)
    time.sleep(.15)
    print('naturally joined',flush=True)
    os._exit(0)
os.close(writer)
os.read(reader,5)
os.close(reader)
os._exit(0)
"""
    commands, argv = native_command(tmp_path, code)
    started = time.monotonic()
    assert commands.run(argv) == "naturally joined\n"
    assert time.monotonic() - started >= 0.15
    state = commands.records[0]["supervisor"]
    assert state["passed"] is True and state["cleanup_complete"] is True
    assert state["terminated"] == [] and state["cleanup_errors"] == []
    assert all(row["exit_code"] == 0 for row in state["reaped"])


@pytest.mark.parametrize(
    "kind", ["nonzero", "signal", "adopted-nonzero", "adopted-signal", "init-term"]
)
def test_real_nonzero_signal_or_init_interruption_never_becomes_success(tmp_path, kind):
    action = "os._exit(7)" if "nonzero" in kind else "os.kill(os.getpid(),9)"
    if kind == "init-term":
        code = (
            "import os,signal,time;os.kill(os.getppid(),signal.SIGTERM);time.sleep(30)"
        )
    elif kind.startswith("adopted"):
        code = (
            "import os,time\nr,w=os.pipe()\nif os.fork()==0:\n"
            " os.close(r);os.setsid();os.write(w,b'ready');os.close(w);time.sleep(.06);"
            + action
            + "\nos.close(w);os.read(r,5);os.close(r);os._exit(0)"
        )
    else:
        code = "import os;" + action
    commands, argv = native_command(tmp_path, code)
    with pytest.raises(ValueError):
        commands.run(argv)
    state = commands.records[0]["supervisor"]
    assert state["passed"] is False and state["cleanup_complete"] is True
    assert state["exit_code"] != 0
    assert state["cleanup_errors"] == []
    assert not state.get("timed_out"), "An interruption must not wait out the deadline"


def test_persistent_detached_child_keeps_init_alive_until_original_deadline(tmp_path):
    code = """import os,time
r,w=os.pipe()
if os.fork()==0:
    os.close(r);os.setsid();os.write(w,b'ready');os.close(w)
    time.sleep(30);os._exit(0)
os.close(w);os.read(r,5);os.close(r);os._exit(0)
"""
    commands, argv = native_command(tmp_path, code, 0.3)
    started = time.monotonic()
    with pytest.raises(ValueError):
        commands.run(argv)
    assert time.monotonic() - started < 1.5
    state = commands.records[0]["supervisor"]
    assert state["timed_out"] is True and state["passed"] is False
    assert state["cleanup_complete"] is True and state["cleanup_errors"] == []
    assert state["terminated"] and any(row["exit_code"] != 0 for row in state["reaped"])


@pytest.mark.parametrize("optimization", ["-O", "-OO"])
@pytest.mark.parametrize("payload", [[], ["invalid"], ["--"], ["--", "relative"]])
def test_malformed_namespace_init_invocation_refuses_optimized_execution(
    tmp_path, optimization, payload
):
    commands, argv = native_command(tmp_path, "raise RuntimeError('must not run')")
    split = argv.index("--")
    prefix = argv[: split + 1] + [
        argv[split + 1],
        "-I",
        optimization,
        "-B",
        argv[split + 4],
    ]
    with pytest.raises(ValueError):
        commands.run([*prefix, *payload])
    assert commands.records[0]["supervisor"]["exit_code"] == 2
    assert commands.records[0]["supervisor"]["cleanup_complete"] is True


@pytest.mark.parametrize("optimization", ["-O", "-OO"])
def test_init_refuses_host_execution_before_spawning_or_reaping(tmp_path, optimization):
    from tools.ci import light_environment_init

    marker = tmp_path / "must-not-spawn"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            optimization,
            light_environment_init.__file__,
            "--",
            "/usr/bin/touch",
            str(marker),
        ],
        capture_output=True,
        text=True,
        timeout=2,
    )
    assert result.returncode == 2 and result.stdout == result.stderr == ""
    assert not marker.exists()


def test_init_keeps_command_child_distinct_and_preserves_file_and_network_isolation(
    tmp_path,
):
    code = """import errno,json,os,pathlib,socket,sys
root=pathlib.Path(sys.argv[1]);marker=root/'downloads/read-only'
assert os.getpid()!=1 and os.getppid()==1
assert not (root/'host-only').exists()
assert marker.read_text()=='sentinel'
try: marker.write_text('bad')
except OSError as error: assert error.errno==errno.EROFS
else: raise RuntimeError('readonly mount was writable')
connection=socket.socket();connection.settimeout(.2)
assert connection.connect_ex(('127.0.0.1',int(sys.argv[2])))!=0
connection.close()
print(json.dumps({'pid':os.getpid(),'ppid':os.getppid(),'net':os.readlink('/proc/self/ns/net')}))
"""
    commands, argv = native_command(tmp_path, code)
    (tmp_path / "host-only").write_text("private synthetic sentinel")
    (tmp_path / "downloads/read-only").write_text("sentinel")
    with socket_listener() as listener:
        data = json.loads(
            commands.run([*argv, str(tmp_path), str(listener.getsockname()[1])])
        )
    assert data["pid"] != 1 and data["ppid"] == 1
    assert data["net"] != os.readlink("/proc/self/ns/net")
    assert (tmp_path / "downloads/read-only").read_text() == "sentinel"


def socket_listener():
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    return listener


@pytest.mark.parametrize("damage", ["missing", "linked", "changed"])
def test_selected_init_helper_changes_are_rejected(
    subject, repository, tmp_path, boundary, monkeypatch, damage
):
    from tools.ci import light_environment_init

    boundary["root"] = repository
    args = arguments(repository, tmp_path)
    subject.run(args)
    replacement = tmp_path / "selected-init.py"
    replacement.write_bytes(Path(light_environment_init.__file__).read_bytes())
    if damage == "missing":
        replacement.unlink()
    elif damage == "linked":
        replacement.unlink()
        replacement.symlink_to(light_environment_init.__file__)
    else:
        replacement.write_text("raise SystemExit(0)\n")
    monkeypatch.setattr(light_environment_init, "__file__", str(replacement))
    with pytest.raises(ValueError, match="helper"):
        subject.selection(repository, args.output, args.revision)


@pytest.mark.parametrize("damage", ["missing", "record", "abi"])
def test_init_base_interpreter_identity_is_required_and_revalidated(
    tmp_path, monkeypatch, damage
):
    commands, _ = native_command(tmp_path, "pass")
    record = sandbox.identity(commands)
    if damage == "missing":
        monkeypatch.setattr(sys, "_base_executable", str(tmp_path / "missing-python"))
        with pytest.raises(FileNotFoundError):
            sandbox.unchanged(record)
    elif damage == "record":
        record["init"]["python_sha256"] = "0" * 64
        with pytest.raises(ValueError, match="interpreter changed"):
            sandbox.unchanged(record)
    else:
        monkeypatch.setattr(
            sandbox.sysconfig, "get_config_var", lambda _key: "different-abi"
        )
        with pytest.raises(ValueError, match="ABI"):
            sandbox.identity(commands)


OWNED_EVENTS = r'''
import ctypes,json,os,pathlib,select,signal,subprocess,sys,time
base=pathlib.Path(sys.argv[1]);root=sys.argv[2];mode=sys.argv[3]
def identity(pid):
    f=pathlib.Path(f'/proc/{pid}/stat').read_text().rpartition(') ')[2].split()
    return int(f[1]),int(f[19])
def until(predicate):
    end=time.monotonic()+3
    while time.monotonic()<end:
        if predicate():return
        time.sleep(.002)
    raise TimeoutError('owned fixture event expired')
def worker():
    sys.path.insert(0,root)
    from tools.ci import light_environment_sandbox as sandbox
    from tools.ci.light_environment_process import Commands
    from tools.ci.cancellation import cancellation_scope
    if mode=='early':
        actual=subprocess.Popen
        gate="import pathlib,runpy,sys,time; b=pathlib.Path(sys.argv[1]); (b/'early-ready').write_text('ready'); end=time.monotonic()+3\nwhile not (b/'release').exists() and time.monotonic()<end: time.sleep(.002)\nsys.argv=sys.argv[2:];runpy.run_path(sys.argv[0],run_name='__main__')"
        def delayed(argv,**kwargs):return actual([sys.executable,'-I','-B','-c',gate,str(base),*argv[2:]],**kwargs)
        subprocess.Popen=delayed
    payload="""import os,pathlib,sys,time
marker=pathlib.Path(sys.argv[1]);mode=sys.argv[2]
if os.fork()==0:
    os.setsid()
    if mode=='settlement':
        end=time.monotonic()+2
        while os.getppid()!=1 and time.monotonic()<end:time.sleep(.002)
        if os.getppid()!=1:os._exit(17)
    marker.write_text(str(os.getpid()))
    time.sleep(30);os._exit(0)
if mode=='settlement':os._exit(0)
time.sleep(30)
"""
    argv=[*sandbox.command(base/'builder',base/'downloads',base/'wheels',base/'lock'),'--',sandbox.init_material()['python'],'-I','-B','-c',payload,str(base/'wheels/ready'),mode]
    with cancellation_scope() as latch:
        try:Commands(base/'logs',5).run(argv)
        except (Exception,KeyboardInterrupt) as error:
            (base/'worker-result.json').write_text(json.dumps({'error':type(error).__name__,'signals':latch.signals}))
def outer():
    if ctypes.CDLL(None,use_errno=True).prctl(36,1,0,0,0):raise OSError('fixture subreaper unavailable')
    for name in ('builder','downloads','wheels','logs'):(base/name).mkdir()
    (base/'lock').write_text('owned fixture')
    worker=subprocess.Popen([sys.executable,'-I','-B',__file__,str(base),root,mode,'worker'])
    descriptor=os.pidfd_open(worker.pid);handles={};observed=[];reaped=[];rescue=False;started=time.monotonic()
    def snapshot():
        queue=[worker.pid];seen=set()
        while queue:
            pid=queue.pop()
            if pid in seen:continue
            seen.add(pid);fd=None
            try:
                before=identity(pid);key=f'{pid}:{before[1]}'
                if key not in handles:
                    fd=os.pidfd_open(pid)
                    if identity(pid)!=before:raise RuntimeError('fixture identity changed')
                    handles[key]=fd;fd=None
                state=pathlib.Path(f'/proc/{pid}/status').read_text()
                observed.append({'pid':pid,'ppid':before[0],'start_ticks':before[1],'nspid':next(x for x in state.splitlines() if x.startswith('NSpid:'))})
                queue.extend(map(int,pathlib.Path(f'/proc/{pid}/task/{pid}/children').read_text().split()))
            except (FileNotFoundError,ProcessLookupError):pass
            finally:
                if fd is not None:os.close(fd)
    try:
        marker=base/('early-ready' if mode=='early' else 'wheels/ready')
        until(marker.exists);snapshot()
        before=identity(worker.pid)
        if before[0]!=os.getpid() or identity(worker.pid)!=before:raise RuntimeError('original worker identity changed')
        if mode=='cancel':
            for event in (signal.SIGTERM,signal.SIGINT,signal.SIGTERM,signal.SIGINT):
                try:signal.pidfd_send_signal(descriptor,event)
                except ProcessLookupError:break
                time.sleep(.001)
        else:
            signal.pidfd_send_signal(descriptor,signal.SIGKILL);worker.wait(timeout=.5)
            if mode=='early':(base/'release').write_text('caller gone before helper begins')
        end=time.monotonic()+1
        while time.monotonic()<end:
            try:pid,status=os.waitpid(-1,os.WNOHANG)
            except ChildProcessError:break
            if pid:
                reaped.append({'pid':pid,'exit_code':os.waitstatus_to_exitcode(status)})
                if pid==worker.pid:worker.returncode=os.waitstatus_to_exitcode(status)
            time.sleep(.002)
        else:raise TimeoutError('production cleanup exceeded fixture bound')
    finally:
        end=time.monotonic()+2
        while time.monotonic()<end:
            try:pid,status=os.waitpid(-1,os.WNOHANG)
            except ChildProcessError:break
            if pid:
                if pid==worker.pid:worker.returncode=os.waitstatus_to_exitcode(status)
                reaped.append({'pid':pid,'exit_code':os.waitstatus_to_exitcode(status)});continue
            for raw in pathlib.Path(f'/proc/self/task/{os.getpid()}/children').read_text().split():
                pid=int(raw);fd=None
                try:
                    before=identity(pid);fd=os.pidfd_open(pid)
                    if before[0]==os.getpid() and identity(pid)==before:
                        rescue=True;signal.pidfd_send_signal(fd,signal.SIGKILL)
                except (FileNotFoundError,ProcessLookupError):pass
                finally:
                    if fd is not None:os.close(fd)
            time.sleep(.005)
        else:raise RuntimeError('fixture rescue incomplete')
        ready={key:bool(select.select([fd],[],[],0)[0]) for key,fd in handles.items()}
        for fd in handles.values():os.close(fd)
        os.close(descriptor)
        result={'rescue':rescue,'elapsed':time.monotonic()-started,'observed':observed,'terminal_pidfds':ready,'outer_reaped':reaped,'supervisor':json.loads((base/'logs/command-000.supervisor.json').read_text())}
        if (base/'worker-result.json').exists():result['worker']=json.loads((base/'worker-result.json').read_text())
        (base/'event-result.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result))
if sys.argv[-1]=='worker':worker()
else:outer()
'''


@pytest.mark.parametrize("event", ["early", "active", "settlement", "cancel"])
def test_new_topology_caller_loss_and_repeated_cancel_reap_without_rescue(
    tmp_path, event
):
    script = tmp_path / "owned-events.py"
    script.write_text(OWNED_EVENTS)
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(script), str(tmp_path), str(ROOT), event],
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["rescue"] is False and data["elapsed"] < 1.5
    assert data["terminal_pidfds"] and all(data["terminal_pidfds"].values())
    state = data["supervisor"]
    assert state["passed"] is False and state["cleanup_complete"] is True
    assert state["cleanup_errors"] == []
    if event == "early":
        assert "command_pid" not in state
        assert not (tmp_path / "wheels/ready").exists()
    elif event == "cancel":
        assert len(data["worker"]["signals"]) >= 2
    else:
        assert state["parent_dead"] is True


def test_init_payload_spawn_failure_cannot_become_success(tmp_path):
    commands, argv = native_command(tmp_path, "pass")
    first = argv.index("--")
    second = argv.index("--", first + 1)
    with pytest.raises(ValueError):
        commands.run([*argv[: second + 1], "/nonexistent-klukai-init-payload"])
    state = commands.records[0]["supervisor"]
    assert state["exit_code"] == 1 and state["passed"] is False
    assert state["cleanup_complete"] is True


@pytest.mark.parametrize("optimization", ["-O", "-OO"])
def test_changed_imported_init_is_rejected_before_any_launch_or_mount(
    tmp_path, optimization
):
    code = """import json,pathlib,sys
sys.path.insert(0,sys.argv[1])
from tools.ci import light_environment_init as init, light_environment_sandbox as sandbox
from tools.ci.light_environment_process import Commands
base=pathlib.Path(sys.argv[2])
for name in ('builder','downloads','wheels','logs'):(base/name).mkdir()
(base/'lock').write_text('fixture')
marker=base/'wheels/tampered-init-executed'
replacement=base/'changed-init.py'
replacement.write_text('import pathlib;pathlib.Path('+repr(str(marker))+').write_text("executed")\\n')
init.__file__=str(replacement)
commands=Commands(base/'logs',1);error=None
try:
    argv=sandbox.command(base/'builder',base/'downloads',base/'wheels',base/'lock')
    commands.run([*argv,'--','/usr/bin/true'])
except ValueError as failure:error=str(failure)
print(json.dumps({'error':error,'launches':len(commands.records),'marker':marker.exists()}))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            optimization,
            "-B",
            "-c",
            code,
            str(ROOT),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=4,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["error"] == "Native init differs from imported helper bytes"
    assert data["launches"] == 0 and data["marker"] is False


def test_payload_preserves_original_exec_signal_defaults_and_mask(tmp_path):
    commands, argv = native_command(tmp_path, "pass")
    first = argv.index("--")
    second = argv.index("--", first + 1)
    direct = commands.run([*argv[: first + 1], "/usr/bin/cat", "/proc/self/status"])
    through_init = commands.run(
        [*argv[: second + 1], "/usr/bin/cat", "/proc/self/status"]
    )

    def dispositions(status):
        return {
            line.split(":")[0]: int(line.split()[1], 16)
            for line in status.splitlines()
            if line.startswith(("SigIgn:", "SigBlk:"))
        }

    before, after = dispositions(direct), dispositions(through_init)
    assert before["SigIgn"] & (1 << 12) == 0  # Linux SIGPIPE13 defaults to terminate.
    assert before["SigBlk"] == 0
    assert after == before
