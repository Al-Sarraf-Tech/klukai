"""Only known interpreter aliases may escape an inventoried venv."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ALIAS = "\U0001d70bthon"


@pytest.fixture
def installed():
    from tools.ci import light_environment_installed

    return light_environment_installed


@pytest.fixture
def real_venv(tmp_path):
    host = Path(sys.executable).resolve(strict=True)
    directory = tmp_path / "real venv"
    argv = [str(host), "-I", "-B", "-m", "venv", "--without-pip", str(directory)]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    alias = directory / "bin" / ALIAS
    automatic_alias = alias.is_symlink()
    if not automatic_alias:
        # Older supported CPython versions lack the automatic 3.14 alias.
        # Exercise the same exact allowed name/host rule without skipping it.
        alias.symlink_to(host)
    links = {
        path.relative_to(directory).as_posix(): {
            "link": os.readlink(path),
            "target": str(path.resolve(strict=True)),
        }
        for path in directory.rglob("*")
        if path.is_symlink()
    }
    (tmp_path / "creation.json").write_text(
        json.dumps(
            {
                "argv": argv,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "host": str(host),
                "host_sha256": hashlib.sha256(host.read_bytes()).hexdigest(),
                "filesystem_encoding": sys.getfilesystemencoding(),
                "automatic_alias": automatic_alias,
                "links": links,
            },
            indent=2,
        )
    )
    return directory, host, alias


def test_real_stdlib_venv_alias_binds_complete_trusted_target_bytes(
    installed, real_venv, tmp_path
):
    directory, host, alias = real_venv
    assert alias.resolve(strict=True) == host
    captured = installed.capture(directory, host)
    installed.unchanged(directory, host, captured)
    row = captured[f"bin/{ALIAS}"]
    assert row["link"] == os.readlink(alias)
    assert row["target"] == str(host)
    assert row["target_file"]["sha256"] == hashlib.sha256(host.read_bytes()).hexdigest()
    info = host.stat()
    assert row["target_file"]["metadata"] == {
        field: getattr(info, "st_" + field)
        for field in (
            "dev",
            "ino",
            "mode",
            "uid",
            "gid",
            "nlink",
            "size",
            "mtime_ns",
            "ctime_ns",
        )
    }
    (tmp_path / "captured.json").write_text(json.dumps(captured, indent=2))


def test_exact_alias_to_different_external_file_fails(installed, real_venv, tmp_path):
    directory, host, alias = real_venv
    installed.capture(directory, host)  # Establish a valid control before damage.
    different = tmp_path / "untrusted interpreter"
    different.write_text("unrelated external executable fixture")
    different.chmod(0o700)
    alias.unlink()
    alias.symlink_to(different)
    with pytest.raises(ValueError, match="link escapes"):
        installed.capture(directory, host)


@pytest.mark.parametrize(
    "name",
    [
        "not-python",
        "\u03c0thon",
        "\U0001d6d1thon",
        "\uff50ython3.14",
        "python\u0663.\u0661\u0664",
        "python3.14-extra",
    ],
)
def test_unrelated_and_unicode_lookalike_names_cannot_target_host(
    installed, real_venv, name
):
    directory, host, _alias = real_venv
    installed.capture(directory, host)
    (directory / "bin" / name).symlink_to(host)
    with pytest.raises(ValueError, match="link escapes"):
        installed.capture(directory, host)


def test_exact_alias_outside_bin_cannot_target_host(installed, real_venv):
    directory, host, _alias = real_venv
    installed.capture(directory, host)
    outside = directory / "share"
    outside.mkdir()
    (outside / ALIAS).symlink_to(host)
    with pytest.raises(ValueError, match="link escapes"):
        installed.capture(directory, host)


@pytest.mark.parametrize("damage", ["repoint-foreign", "same-target", "delete"])
def test_captured_alias_link_mutation_fails_revalidation(
    installed, real_venv, tmp_path, damage
):
    directory, host, alias = real_venv
    captured = installed.capture(directory, host)
    alias.unlink()
    if damage == "repoint-foreign":
        different = tmp_path / "other external file"
        different.write_text("changed target")
        alias.symlink_to(different)
    elif damage == "same-target":
        # Preserve resolved host bytes but change the captured link text/identity.
        alias.symlink_to(directory / "bin/python")
    with pytest.raises(ValueError):
        installed.unchanged(directory, host, captured)
