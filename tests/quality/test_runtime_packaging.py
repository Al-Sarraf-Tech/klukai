"""Deterministic wheel manifests and exact native dependency selection."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile

import pytest


def load_packaging():
    path = Path(__file__).resolve().parents[2] / "docker/core/package_runtime.py"
    assert path.is_file(), "runtime packaging implementation is missing"
    spec = importlib.util.spec_from_file_location("klukai_runtime_packaging", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wheel(path: Path, name: str, version: str) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            f"{name}-{version}.dist-info/METADATA",
            f"Name: {name}\nVersion: {version}\n",
        )


def test_wheel_lock_uses_actual_metadata_and_content_hash(tmp_path):
    packaging = load_packaging()
    wheel(tmp_path / "z-1.whl", "Some_Name", "1.2")
    wheel(tmp_path / "a-1.whl", "Another.Name", "3.4")
    lock = tmp_path / "runtime.lock"
    manifest = tmp_path / "materials.json"
    packaging.wheel_lock(tmp_path, lock, manifest)
    lines = lock.read_text().splitlines()
    assert lines[0].startswith("another-name==3.4 --hash=sha256:")
    assert (
        lines[1]
        == "some-name==1.2 --hash=sha256:"
        + hashlib.sha256((tmp_path / "z-1.whl").read_bytes()).hexdigest()
    )
    assert len(json.loads(manifest.read_text())["wheels"]) == 2


def test_duplicate_wheel_distribution_fails_closed(tmp_path):
    packaging = load_packaging()
    wheel(tmp_path / "one.whl", "Same_Name", "1.2")
    wheel(tmp_path / "two.whl", "same-name", "1.3")
    with pytest.raises(ValueError, match="duplicate"):
        packaging.wheel_lock(tmp_path, tmp_path / "lock", tmp_path / "manifest")


def test_vendored_distribution_metadata_does_not_replace_wheel_identity(tmp_path):
    packaging = load_packaging()
    path = tmp_path / "setuptools-84.0.0-py3-none-any.whl"
    wheel(path, "setuptools", "84.0.0")
    # Setuptools legitimately bundles vendor metadata inside its package.
    with ZipFile(path, "a") as archive:
        archive.writestr(
            "setuptools/_vendor/packaging-26.3.dist-info/METADATA",
            "Name: packaging\nVersion: 26.3\n",
        )
    lock, manifest = tmp_path / "lock", tmp_path / "manifest"
    packaging.wheel_lock(tmp_path, lock, manifest)
    assert lock.read_text().startswith("setuptools==84.0.0 --hash=sha256:")
    assert [item["name"] for item in json.loads(manifest.read_text())["wheels"]] == [
        "setuptools"
    ]


def test_multiple_top_level_wheel_identities_are_still_rejected(tmp_path):
    packaging = load_packaging()
    path = tmp_path / "ambiguous.whl"
    wheel(path, "one", "1")
    with ZipFile(path, "a") as archive:
        archive.writestr("two-2.dist-info/METADATA", "Name: two\nVersion: 2\n")
    with pytest.raises(ValueError, match="invalid wheel metadata"):
        packaging.wheel_lock(tmp_path, tmp_path / "lock", tmp_path / "manifest")


def test_ldd_parser_retains_direct_loader_and_mapped_dependencies():
    packaging = load_packaging()
    parsed = packaging.parse_ldd(
        "linux-vdso.so.1 (0x00)\n"
        "libssl.so.3 => /usr/lib/x86_64-linux-gnu/libssl.so.3 (0x00)\n"
        "/lib64/ld-linux-x86-64.so.2 (0x00)\n"
    )
    assert parsed == {
        Path("/usr/lib/x86_64-linux-gnu/libssl.so.3"),
        Path("/lib64/ld-linux-x86-64.so.2"),
    }


def test_unresolved_native_dependency_is_not_ignored():
    packaging = load_packaging()
    with pytest.raises(ValueError, match="unresolved"):
        packaging.parse_ldd("libpq.so.5 => not found\n")


def test_dependency_selection_preserves_base_and_owned_venv_libraries():
    packaging = load_packaging()
    paths = {
        Path("/usr/lib/libssl.so.3"),
        Path("/usr/lib/libsqlite3.so.0"),
        Path("/opt/venv/lib/native.so"),
    }
    selected = packaging.missing_libraries(paths, {"libssl.so.3"}, (Path("/opt/venv"),))
    assert selected == {Path("/usr/lib/libsqlite3.so.0")}


def test_native_closure_ignores_nonloadable_relocatable_objects(tmp_path):
    packaging = load_packaging()
    # ELF64 little-endian, ET_REL. A compiler object is not a runtime library.
    (tmp_path / "build-only.o").write_bytes(
        b"\x7fELF\x02\x01" + b"\0" * 10 + b"\x01\0" + b"\0" * 46
    )
    assert packaging.elf_dependencies((tmp_path,)) == set()


def test_native_closure_resolves_dependencies_bundled_with_actual_database_wheel():
    packaging = load_packaging()
    spec = importlib.util.find_spec("psycopg_binary")
    assert spec and spec.submodule_search_locations
    package = Path(next(iter(spec.submodule_search_locations)))
    dependencies = packaging.elf_dependencies(
        (package, package.with_name("psycopg_binary.libs"))
    )
    assert any("libkrb5" in path.name for path in dependencies)
