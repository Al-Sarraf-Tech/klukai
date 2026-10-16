"""Production credentials retain exact values and fail closed on bad input."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "disposable-test-" + "x" * 40


def store():
    path = ROOT / "scripts/release/secret_store.py"
    assert path.is_file(), "encrypted credential consumer is missing"
    spec = importlib.util.spec_from_file_location("klukai_secret_store", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def credential(path: Path, **updates) -> Path:
    data = {
        "schema_version": 1,
        "environment": {"POSTGRES_PASSWORD": "test-only $'\\\nvalue"},
        "metrics_token": TOKEN,
    }
    data.update(updates)
    path.write_text(json.dumps(data))
    path.chmod(0o400)
    return path


def test_credential_preserves_environment_values_without_shell_or_dotenv(tmp_path):
    module = store()
    path = credential(tmp_path / "credential")
    payload = module.load_credential(path)
    result = module.merge_environment(
        payload, {"PATH": "/usr/bin", "POSTGRES_PASSWORD": "old", "KEEP": "yes"}
    )
    assert result["POSTGRES_PASSWORD"] == "test-only $'\\\nvalue"
    assert result["PATH"] == "/usr/bin"
    assert result["KEEP"] == "yes"


@pytest.mark.parametrize("mode", [0o444, 0o440, 0o640, 0o644, 0o666])
def test_group_or_world_readable_credential_is_rejected(tmp_path, mode):
    module = store()
    path = credential(tmp_path / "credential")
    path.chmod(mode)
    with pytest.raises(module.CredentialError, match="private"):
        module.load_credential(path)


def test_symlink_and_wrong_owner_credentials_are_rejected(tmp_path):
    module = store()
    path = credential(tmp_path / "credential")
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(module.CredentialError):
        module.load_credential(link)
    with pytest.raises(module.CredentialError, match="owner"):
        module.load_credential(path, uid=os.getuid() + 1)


@pytest.mark.parametrize(
    "update",
    [
        {"schema_version": True},
        {"environment": {}},
        {"environment": {"POSTGRES_PASSWORD": None}},
        {"environment": {"POSTGRES_PASSWORD": "contains\0null"}},
        {"environment": {"LD_PRELOAD": "/tmp/malicious.so"}},
        {"environment": {"PATH": "/tmp"}},
        {"environment": {"DOCKER_HOST": "tcp://elsewhere:2375"}},
        {"metrics_token": ""},
        {"metrics_token": "bad\ntoken"},
        {"unexpected": "field"},
    ],
)
def test_invalid_payload_or_tool_environment_injection_is_rejected(tmp_path, update):
    module = store()
    path = credential(tmp_path / "credential", **update)
    with pytest.raises(module.CredentialError):
        module.load_credential(path)


def test_missing_and_malformed_credentials_fail_without_exposing_content(tmp_path):
    module = store()
    path = tmp_path / "missing"
    with pytest.raises(module.CredentialError):
        module.load_credential(path)
    path.write_text("not-json-with-private-material")
    path.chmod(0o400)
    with pytest.raises(module.CredentialError) as caught:
        module.load_credential(path)
    assert "private-material" not in str(caught.value)


def test_metrics_token_is_dedicated_private_atomic_and_rotation_visible(tmp_path):
    module = store()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    payload = module.load_credential(credential(tmp_path / "credential"))
    token = module.install_metrics_token(payload, runtime)
    assert token == runtime / "metrics-token"
    assert token.read_text() == TOKEN
    assert stat.S_IMODE(token.stat().st_mode) == 0o400
    payload["metrics_token"] = "rotated-test-" + "y" * 40
    module.install_metrics_token(payload, runtime)
    assert token.read_text() == payload["metrics_token"]
    payload["metrics_token"] = TOKEN
    module.install_metrics_token(payload, runtime)
    assert token.read_text() == TOKEN
    assert sorted(p.name for p in runtime.iterdir()) == ["metrics-token"]


def test_metrics_materialization_refuses_public_or_symlink_runtime_directory(tmp_path):
    module = store()
    payload = module.load_credential(credential(tmp_path / "credential"))
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o755)
    with pytest.raises(module.CredentialError):
        module.install_metrics_token(payload, runtime)
    runtime.chmod(0o700)
    link = tmp_path / "link"
    link.symlink_to(runtime, target_is_directory=True)
    with pytest.raises(module.CredentialError):
        module.install_metrics_token(payload, link)


def test_exec_transfers_exact_values_and_only_public_runtime_path_in_arguments(
    tmp_path,
):
    module = store()
    credentials = tmp_path / "credentials"
    credentials.mkdir(mode=0o700)
    credential(credentials / module.CREDENTIAL_ID)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    child = "import os,json,pathlib,sys; expected=json.loads(pathlib.Path(sys.argv[1]).read_text())['environment']; assert all(os.environ[k]==v for k,v in expected.items()); assert all(v.encode() not in pathlib.Path('/proc/self/cmdline').read_bytes() for v in expected.values()); assert os.environ['KLUKAI_RUNTIME_SECRETS_DIR']; print('exact environment preserved')"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/release/secret_store.py"),
            "--",
            sys.executable,
            "-c",
            child,
            str(credentials / module.CREDENTIAL_ID),
        ],
        env={
            **os.environ,
            "CREDENTIALS_DIRECTORY": str(credentials),
            "RUNTIME_DIRECTORY": str(runtime),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "exact environment preserved"
    assert not result.stderr
    assert (runtime / "metrics-token").read_text() == TOKEN


@pytest.mark.timeout(2)
def test_nonregular_fifo_cannot_block_credential_validation(tmp_path):
    module = store()
    path = tmp_path / "fifo"
    os.mkfifo(path, mode=0o400)
    with pytest.raises(module.CredentialError):
        module.load_credential(path)


def test_bad_credentials_prevent_child_execution_without_logging_values(tmp_path):
    module = store()
    credentials = tmp_path / "credentials"
    credentials.mkdir(mode=0o700)
    path = credential(credentials / module.CREDENTIAL_ID)
    path.chmod(0o644)
    marker = tmp_path / "must-not-exist"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/release/secret_store.py"),
            "--",
            sys.executable,
            "-c",
            f"open({str(marker)!r},'w').close()",
        ],
        env={**os.environ, "CREDENTIALS_DIRECTORY": str(credentials)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert not marker.exists()
    assert not result.stdout
    assert result.stderr.strip() == "klukai credential validation or execution failed"
