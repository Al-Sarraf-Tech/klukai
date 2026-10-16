"""Offline pip receives only its validated public CA material."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_light_environment import arguments
from test_light_environment import boundary as boundary
from test_light_environment import repository as repository
from test_light_environment import subject as subject
from test_light_environment_integrity import export_action


@pytest.fixture
def ca():
    from tools.ci import light_environment_ca

    return light_environment_ca


@pytest.fixture
def system_ca():
    for value in (
        "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/ssl/ca-bundle.pem",
    ):
        path = Path(value)
        if path.is_file():
            return path
    pytest.fail("A legitimate system public CA bundle is required for this fixture")


@pytest.fixture
def bundled(tmp_path, system_ca):
    builder = tmp_path / "trusted builder"
    path = builder / "lib/python3.14/site-packages/pip/_vendor/certifi/cacert.pem"
    path.parent.mkdir(parents=True)
    shutil.copyfile(system_ca, path)
    return builder, path


def test_inside_builder_public_bundle_is_accepted_and_bound(ca, bundled):
    builder, path = bundled
    record = ca.inspect(path, builder)
    assert record["kind"] == "bundled"
    assert record["resolved"] == str(path)
    assert record["size"] == path.stat().st_size
    assert record["certificates"] > 0
    ca.unchanged(record, builder)
    assert ca.mounts(record, builder) == []


@pytest.mark.parametrize(
    "damage", ["missing", "changed", "writable", "escape", "private-key", "extra-text"]
)
def test_invalid_or_changed_public_bundle_fails_closed(ca, bundled, system_ca, damage):
    builder, path = bundled
    before = ca.inspect(path, builder)
    if damage == "missing":
        path.unlink()
    elif damage == "changed":
        path.write_text(path.read_text() + "\n# changed public bundle\n")
    elif damage == "writable":
        path.chmod(0o666)
    elif damage == "escape":
        path.unlink()
        path.symlink_to(system_ca)
    elif damage == "private-key":
        # Relabel public certificates; the fixture contains no private-key bytes.
        path.write_text(path.read_text().replace("CERTIFICATE", "PRIVATE KEY"))
    else:
        path.write_text(path.read_text() + "\nsecret-like arbitrary trailing text\n")
    with pytest.raises((ValueError, OSError)):
        ca.unchanged(before, builder)


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "/proc/self/environ", "/dev/null", "/home/account/ca.pem"]
)
def test_external_paths_outside_public_trust_layouts_are_not_admitted(
    ca, tmp_path, path
):
    builder = tmp_path / "builder"
    builder.mkdir()
    with pytest.raises((ValueError, OSError)):
        ca.inspect(Path(path), builder)


def test_external_user_copy_or_alias_cannot_inject_material(ca, tmp_path, system_ca):
    copied = tmp_path / "account material.pem"
    shutil.copyfile(system_ca, copied)
    alias = tmp_path / "system alias"
    alias.symlink_to(system_ca)
    builder = tmp_path / "builder"
    builder.mkdir()
    for path in (copied, alias):
        with pytest.raises(ValueError):
            ca.inspect(path, builder)


def test_system_bundle_is_visible_readonly_with_tls_verification_and_no_etc_exposure(
    ca, tmp_path, system_ca
):
    from tools.ci import light_environment_sandbox as sandbox

    builder, downloads, wheels = (
        tmp_path / name for name in ("builder", "downloads", "wheels")
    )
    for directory in (builder, downloads, wheels):
        directory.mkdir()
    lock = tmp_path / "lock"
    lock.write_text("fixture")
    record = ca.inspect(system_ca, builder)
    code = """import json,os,pathlib,ssl,sys
path=pathlib.Path(sys.argv[1])
context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
context.load_verify_locations(cafile=str(path))
if not os.statvfs(path).f_flag & os.ST_RDONLY: raise SystemExit('CA material was writable')
if pathlib.Path('/etc/passwd').exists(): raise SystemExit('broad etc exposure')
if context.verify_mode!=ssl.CERT_REQUIRED or not context.check_hostname: raise SystemExit('TLS disabled')
print(json.dumps({'verified':True,'net':os.readlink('/proc/self/ns/net')}))
"""
    result = subprocess.run(
        [
            *sandbox.command(builder, downloads, wheels, lock, ca=record),
            "--",
            str(Path(sys.executable).resolve()),
            "-I",
            "-B",
            "-c",
            code,
            record["requested"],
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["verified"] is True
    assert observed["net"] != os.readlink("/proc/self/ns/net")


@pytest.mark.parametrize("damage", ["missing", "changed", "unsafe-record"])
def test_exact_action_never_exports_after_ca_material_or_record_changes(
    subject, repository, tmp_path, boundary, ca, system_ca, damage
):
    args = arguments(repository, tmp_path)
    boundary.update(root=repository, ca_bundle=True)
    subject.run(args)
    manifest = args.output / "manifest.json"
    report = json.loads(manifest.read_text())
    path = Path(report["builder_ca"]["requested"])
    if damage == "missing":
        path.unlink()
    elif damage == "changed":
        path.write_text(path.read_text() + "\n# changed after completed bootstrap\n")
    else:
        report["builder_ca"]["requested"] = "/etc/passwd"
        manifest.write_text(json.dumps(report))
    result, selected, exported = export_action(tmp_path, repository, args)
    assert result.returncode != 0
    assert not selected.exists() and not exported.exists()


@pytest.mark.parametrize("damage", ["owner", "writable-file", "writable-parent"])
def test_system_ca_requires_root_ownership_and_safe_ancestors(
    ca, tmp_path, system_ca, monkeypatch, damage
):
    builder = tmp_path / "builder"
    builder.mkdir()
    ca.inspect(system_ca, builder)
    actual = Path.stat
    target = system_ca.parent if damage == "writable-parent" else system_ca

    def changed(path, *args, **kwargs):
        info = actual(path, *args, **kwargs)
        if path == target:
            fields = list(info)
            if damage == "owner":
                fields[4] = os.getuid()
            else:
                fields[0] |= 0o020
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "stat", changed)
    with pytest.raises(ValueError):
        ca.inspect(system_ca, builder)
