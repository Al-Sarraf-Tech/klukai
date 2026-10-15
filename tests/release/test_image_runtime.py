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
print(json.dumps({'uid':os.getuid(),'python':sys.version_info[:2],
 'shells':[p for p in ['/bin/sh','/bin/bash','/busybox/sh','/bin/busybox'] if pathlib.Path(p).exists()],
 'installers':[p for p in ['pip','ensurepip'] if importlib.util.find_spec(p) is not None],
 'native_runtime_passed':True}))
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
            "--entrypoint",
            "/opt/venv/bin/python",
            image,
            "-c",
            PROBE,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return inspected, json.loads(result.stdout)


def test_distroless_candidate_has_no_shell_or_package_installer(candidate):
    _, data = candidate
    assert data["shells"] == []
    assert data["installers"] == []


def test_candidate_retains_native_python_abi_and_data_libraries(candidate):
    _, data = candidate
    assert data["python"] == [3, 14]
    assert data["native_runtime_passed"] is True
    assert data["uid"] not in (0, "root")


def test_candidate_healthcheck_runs_without_a_shell(candidate):
    image, _ = candidate
    check = image["Config"]["Healthcheck"]["Test"]
    assert check[0] == "CMD"
    assert check[1] == "/opt/venv/bin/python"
    assert "healthcheck.py" in check[2]
