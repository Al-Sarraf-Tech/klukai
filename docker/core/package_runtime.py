"""Package the pinned CPython ABI and verified wheels for a distroless base.

Runs only in the builder. The resulting native manifest retains provenance and
Debian package metadata for libraries absent from the selected runtime base.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile


def wheel_lock(directory: Path, lock: Path, manifest: Path) -> None:
    records: dict[str, dict[str, str]] = {}
    for path in sorted(directory.glob("*.whl")):
        with ZipFile(path) as archive:
            names = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            if len(names) != 1:
                raise ValueError(f"invalid wheel metadata: {path.name}")
            metadata = BytesParser().parsebytes(archive.read(names[0]))
        name = re.sub(r"[-_.]+", "-", str(metadata["Name"])).lower()
        version = str(metadata["Version"])
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or not re.fullmatch(
            r"[a-zA-Z0-9.!+_-]+", version
        ):
            raise ValueError(f"invalid wheel identity: {path.name}")
        if name in records:
            raise ValueError(f"duplicate wheel distribution: {name}")
        records[name] = {
            "name": name,
            "version": version,
            "file": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    if not records:
        raise ValueError("empty wheel directory")
    wheels = [records[name] for name in sorted(records)]
    lock.write_text(
        "".join(
            f"{row['name']}=={row['version']} --hash=sha256:{row['sha256']}\n"
            for row in wheels
        )
    )
    manifest.write_text(json.dumps({"wheels": wheels}, indent=2) + "\n")


def parse_ldd(output: str) -> set[Path]:
    dependencies: set[Path] = set()
    for line in output.splitlines():
        if "=> not found" in line:
            raise ValueError(f"unresolved native dependency: {line.strip()}")
        match = re.search(r"(?:=>\s+)?(/\S+)\s+\(", line.strip())
        if match:
            dependencies.add(Path(match.group(1)))
    return dependencies


def missing_libraries(
    paths: set[Path], provided_names: set[str], owned_roots: tuple[Path, ...]
) -> set[Path]:
    return {
        path
        for path in paths
        if path.name not in provided_names
        and not any(path.is_relative_to(root) for root in owned_roots)
    }


def copy_python(destination: Path) -> None:
    prefix = destination / "usr/local"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "lib").mkdir()
    shutil.copy2("/usr/local/bin/python3.14", prefix / "bin/python3.14")
    for name in ("python", "python3"):
        (prefix / "bin" / name).symlink_to("python3.14")
    excluded = shutil.ignore_patterns(
        "__pycache__",
        "*.pyc",
        "*.a",
        "*.o",
        "site-packages",
        "ensurepip",
        "idlelib",
        "tkinter",
        "_tkinter*",
        "_test*",
        "_curses*",
        "readline*",
        "_uuid*",
        "test",
        "tests",
        "turtledemo",
    )
    shutil.copytree(
        "/usr/local/lib/python3.14", prefix / "lib/python3.14", ignore=excluded
    )
    (prefix / "lib/python3.14/site-packages").mkdir()
    for path in Path("/usr/local/lib").glob("libpython*.so*"):
        shutil.copy2(path, prefix / "lib" / path.name, follow_symlinks=True)


def elf_dependencies(roots: tuple[Path, ...]) -> set[Path]:
    binaries: list[Path] = []
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            with path.open("rb") as stream:
                header = stream.read(18)
                if header[:4] != b"\x7fELF":
                    continue
            byte_order = "little" if header[5:6] == b"\x01" else "big"
            if int.from_bytes(header[16:18], byte_order) not in {2, 3}:
                continue  # ET_REL objects and other nonloadable build data.
            binaries.append(path)
    # Auditwheel libraries can rely on their importing extension's RPATH.
    # Discover their closure with the whole installed wheel set in scope;
    # this environment is builder-only and never enters the runtime image.
    environment = dict(os.environ)
    environment["LD_LIBRARY_PATH"] = ":".join(
        sorted({str(path.parent) for path in binaries})
    )
    result: set[Path] = set()
    for path in binaries:
        checked = subprocess.run(
            ["ldd", str(path)],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        if checked.returncode:
            raise ValueError(
                f"cannot inspect ELF dependency closure: {path}: {checked.stderr}"
            )
        try:
            result.update(parse_ldd(checked.stdout))
        except ValueError as error:
            raise ValueError(f"{path}: {error}") from error
    return result


def package_native(base: Path, destination: Path) -> None:
    provided = {
        path.name for path in base.rglob("*") if path.is_file() or path.is_symlink()
    }
    owned = (Path("/usr/local"), Path("/opt/venv"), destination / "usr/local")
    paths = missing_libraries(
        elf_dependencies((destination / "usr/local", Path("/opt/venv"))),
        provided,
        owned,
    )
    libraries = destination / "opt/runtime-libs"
    metadata = destination / "usr/share/klukai-runtime"
    status = destination / "var/lib/dpkg/status.d"
    for folder in (libraries, metadata, status):
        folder.mkdir(parents=True, exist_ok=True)
    packages: set[str] = set()
    records = []
    for path in sorted(paths):
        target = libraries / path.name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if (
            target.exists()
            and hashlib.sha256(target.read_bytes()).hexdigest() != digest
        ):
            raise ValueError(f"ambiguous native library: {path.name}")
        shutil.copy2(path, target, follow_symlinks=True)
        # ldd can return /lib aliases; dpkg on Debian 13 records /usr/lib.
        canonical = str(path.resolve())
        owners = subprocess.check_output(
            ["dpkg-query", "-S", canonical], text=True
        ).splitlines()
        owner = owners[0].rsplit(": ", 1)[0]
        packages.add(owner)
        records.append(
            {
                "source": str(path),
                "target": str(target.relative_to(destination)),
                "sha256": digest,
                "package": owner,
            }
        )
    for package in sorted(packages):
        name = package.split(":", 1)[0]
        document = subprocess.check_output(["dpkg-query", "-s", package], text=True)
        (status / name).write_text(document)
        license_file = Path("/usr/share/doc") / name / "copyright"
        if not license_file.is_file():
            raise ValueError(f"missing native license: {package}")
        shutil.copy2(license_file, metadata / f"{name}.copyright", follow_symlinks=True)
    (metadata / "native.json").write_text(
        json.dumps({"libraries": records}, indent=2) + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("wheels", "runtime"))
    parser.add_argument("--base", type=Path, default=Path("/distroless-root"))
    parser.add_argument("--out", type=Path, default=Path("/runtime"))
    args = parser.parse_args()
    if args.command == "wheels":
        wheel_lock(
            Path("/wheels"), Path("/runtime.lock"), Path("/wheel-materials.json")
        )
    else:
        copy_python(args.out)
        package_native(args.base, args.out)


if __name__ == "__main__":
    main()
