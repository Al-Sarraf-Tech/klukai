"""Exercise the actual candidate image, without provider keys or networking."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

PROBE = r"""
import asyncio,bcrypt,bz2,ctypes,gzip,importlib.util,io,json,lzma,os,pathlib,sqlite3,ssl,sys,uuid
import psycopg,uvloop,yaml
from PIL import Image
image=Image.new('RGB',(2,2),(12,34,56)); stream=io.BytesIO(); image.save(stream,format='PNG'); stream.seek(0)
assert Image.open(stream).getpixel((0,0))==(12,34,56)
assert bcrypt.checkpw(b'probe',bcrypt.hashpw(b'probe',bcrypt.gensalt(rounds=4)))
assert sqlite3.connect(':memory:').execute('select 1').fetchone()==(1,)
assert bz2.decompress(bz2.compress(b'probe'))==b'probe'
assert lzma.decompress(lzma.compress(b'probe'))==b'probe'
assert gzip.decompress(gzip.compress(b'probe'))==b'probe'
assert ssl.create_default_context().cert_store_stats()['x509_ca']>0
with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner: runner.run(asyncio.sleep(0))
assert yaml.safe_load('a: 1')=={'a':1}
assert psycopg.pq.version()>0
assert importlib.util.find_spec('_tkinter') is None
for unused in ('readline','_curses','_uuid'):
 assert importlib.util.find_spec(unused) is None
assert uuid.UUID(str(uuid.uuid4())).version==4
assert str(uuid.uuid5(uuid.NAMESPACE_DNS,'klukai.example'))=='cb9e73cc-7f6e-59bd-9b4a-41901702f458'
dsos=sorted({line.split()[-1] for line in pathlib.Path('/proc/self/maps').read_text().splitlines() if '/' in line and '.so' in line.split()[-1]})
assert any('libpq' in name for name in dsos)
assert all(name.startswith(('/opt/venv/','/opt/runtime-libs/','/usr/local/','/usr/lib/','/lib/','/lib64/')) for name in dsos)
print(json.dumps({'uid':os.getuid(),'python':sys.version_info[:2],
 'shells':[p for p in ['/bin/sh','/bin/bash','/busybox/sh','/bin/busybox'] if pathlib.Path(p).exists()],
 'installers':[p for p in ['pip','ensurepip'] if importlib.util.find_spec(p) is not None],
 'native_runtime_passed':True,'native_dsos':dsos}))
"""

HEALTHCHECK_PROBE = r"""
import json,subprocess,sys,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
command,scenario=json.loads(sys.argv[1]),sys.argv[2]
seen=[]
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  seen.append(self.path)
  status,body=200,b'{"status":"ok"}'
  if scenario=='outage' and self.path=='/api/health/ready':
   status,body=503,b'{"status":"unhealthy"}'
  elif scenario=='unhealthy': status,body=503,b'{"status":"unhealthy"}'
  elif scenario=='malformed': body=b'[]'
  self.send_response(status);self.end_headers();self.wfile.write(body)
 def log_message(self,*args): pass
with ThreadingHTTPServer(('127.0.0.1',8300),Handler) as server:
 thread=threading.Thread(target=server.serve_forever);thread.start()
 try:
  live=subprocess.run(command,capture_output=True,text=True,timeout=10)
  ready=subprocess.run([*command,'--readiness'],capture_output=True,text=True,timeout=10)
 finally:
  server.shutdown();thread.join(timeout=2)
 assert not thread.is_alive()
print(json.dumps({'scenario':scenario,'default_exit_code':live.returncode,
 'readiness_exit_code':ready.returncode,'requested_paths':seen,
 'stderr':[live.stderr,ready.stderr]}))
"""


@pytest.fixture(scope="module")
def candidate() -> tuple[dict, dict]:
    image = os.environ.get("KLUKAI_TEST_IMAGE")
    assert image, "KLUKAI_TEST_IMAGE must select the actual built candidate"
    inspected = json.loads(
        subprocess.check_output(["docker", "image", "inspect", image], text=True)
    )[0]
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--env",
            "LD_BIND_NOW=1",
            "--entrypoint",
            "/opt/venv/bin/python",
            inspected["Id"],
            "-c",
            PROBE,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    output = os.environ.get("KLUKAI_IMAGE_PROBE_OUTPUT")
    if output:
        from pathlib import Path

        Path(output).write_text(
            json.dumps({"image_id": inspected["Id"], "probe": data}, indent=2) + "\n"
        )
    return inspected, data


def test_distroless_candidate_has_no_shell_or_package_installer(candidate):
    _, data = candidate
    assert data["shells"] == []
    assert data["installers"] == []


def test_candidate_retains_native_python_abi_and_data_libraries(candidate):
    _, data = candidate
    assert data["python"] == [3, 14]
    assert data["native_runtime_passed"] is True
    assert data["uid"] not in (0, "root")


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("healthy", (0, 0)),
        ("outage", (0, 1)),
        ("unhealthy", (1, 1)),
        ("malformed", (1, 1)),
    ],
)
def test_candidate_healthcheck_runs_without_a_shell(candidate, scenario, expected):
    image, _ = candidate
    check = image["Config"]["Healthcheck"]["Test"]
    assert check[0] == "CMD"
    assert check[1] == "/opt/venv/bin/python"
    assert "healthcheck.py" in check[2]
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cpus=1",
            "--memory=256m",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=64",
            "--entrypoint",
            "/opt/venv/bin/python",
            image["Id"],
            "-W",
            "error",
            "-c",
            HEALTHCHECK_PROBE,
            json.dumps(check[1:]),
            scenario,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert (observed["default_exit_code"], observed["readiness_exit_code"]) == expected
    assert observed["requested_paths"] == ["/api/health/live", "/api/health/ready"]
    assert observed["stderr"] == ["", ""]
