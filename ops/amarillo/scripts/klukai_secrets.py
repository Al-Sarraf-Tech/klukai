#!/usr/bin/env python3
"""Klukai production secrets, encrypted at rest with systemd-creds (user scope).

Each secret file is stored as `<name>.cred` under ~/.config/klukai/credentials,
encrypted with `systemd-creds --user` (TPM2 plus host key, bound to this
machine, this user and the credential name). A recovery copy encrypted with
the host key only (`recovery/<name>.cred`) survives a TPM reset on this host;
loss of the host itself needs the owner to re-enter the values. Plaintext exists only in a private
directory under $XDG_RUNTIME_DIR (tmpfs) for the duration of one command and is
deleted afterwards. A missing or undecryptable credential fails closed: there
is no fallback to a plaintext file.

    klukai-secrets migrate compose-env ~/git/klukai/.env     # encrypt, verify, keep .prev
    klukai-secrets rotate core-override new-override.json     # same, for a changed value
    klukai-secrets rollback core-override                     # restore the previous version
    klukai-secrets recover core-override                      # rebuild from the host-key copy
    klukai-secrets check                                      # permissions + every cred decrypts

Values are never printed. Comparisons use SHA-256 in memory only.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
import contextlib
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

STORE = Path.home() / ".config/klukai/credentials"
NAMES = ("compose-env", "core-override")
SYSTEMD_CREDS = "systemd-creds"


class SecretError(RuntimeError):
    """A credential is missing, unreadable or insecure: fail closed."""


def _check_private(path: Path, mode: int) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise SecretError(f"{path} must be a regular path owned by this user")
    if info.st_mode & 0o777 & ~mode:
        raise SecretError(f"{path} permissions are wider than {oct(mode)}")


def _store(store: Path) -> Path:
    if not store.is_dir():
        raise SecretError(f"credential store missing: {store}")
    _check_private(store, 0o700)
    return store


def cred_path(name: str, store: Path = STORE) -> Path:
    if name not in NAMES:
        raise SecretError(f"unknown credential {name!r}")
    return store / f"{name}.cred"


def _creds(*args: str, data: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            [SYSTEMD_CREDS, "--user", *args],
            input=data,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SecretError(f"systemd-creds unavailable: {error}") from error
    if result.returncode:
        raise SecretError(
            f"systemd-creds {args[0]} failed: {result.stderr.decode()[:200]}"
        )
    return result.stdout


def decrypt(name: str, store: Path = STORE) -> bytes:
    path = cred_path(name, _store(store))
    if not path.is_file():
        raise SecretError(f"credential {name} is missing")
    _check_private(path, 0o600)
    return _creds("decrypt", f"--name=klukai-{name}", str(path), "-")


def encrypt(name: str, plaintext: bytes, store: Path = STORE) -> dict[str, object]:
    """Encrypt, read back, compare in memory, then replace and keep `.prev`."""
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    _store(store)
    if not plaintext:
        raise SecretError("refusing to store an empty credential")
    target = cred_path(name, store)
    recovery = store / "recovery"
    recovery.mkdir(mode=0o700, exist_ok=True)
    _check_private(recovery, 0o700)
    staged = _sealed(name, plaintext, store, "auto")
    rescue = _sealed(name, plaintext, recovery, "host")
    if target.exists():
        os.replace(target, target.with_suffix(".cred.prev"))
    os.replace(staged, target)
    os.replace(rescue, recovery / target.name)
    return {"name": name, "bytes": len(plaintext), "values_preserved": True}


def _sealed(name: str, plaintext: bytes, directory: Path, key: str) -> Path:
    """Encrypt into a staged file and prove it decrypts to exactly the input."""
    fd, staged = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".cred")
    os.close(fd)
    try:
        os.chmod(staged, 0o600)
        _creds(
            "encrypt",
            f"--with-key={key}",
            f"--name=klukai-{name}",
            "-",
            staged,
            data=plaintext,
        )
        os.chmod(staged, 0o600)
        back = _creds("decrypt", f"--name=klukai-{name}", staged, "-")
        if hashlib.sha256(back).digest() != hashlib.sha256(plaintext).digest():
            raise SecretError("encrypted credential does not decrypt to the input")
        return Path(staged)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


def recover(name: str, store: Path = STORE) -> dict[str, object]:
    """Rebuild the primary credential from the host-key recovery copy."""
    rescue = _store(store) / "recovery" / cred_path(name, store).name
    if not rescue.is_file():
        raise SecretError(f"no recovery copy of {name}")
    _check_private(rescue, 0o600)
    return encrypt(
        name, _creds("decrypt", f"--name=klukai-{name}", str(rescue), "-"), store
    )


def rollback(name: str, store: Path = STORE) -> None:
    target = cred_path(name, _store(store))
    previous = target.with_suffix(".cred.prev")
    if not previous.is_file():
        raise SecretError(f"no previous version of {name}")
    _check_private(previous, 0o600)
    _creds("decrypt", f"--name=klukai-{name}", str(previous), "-")
    staged = target.with_suffix(".cred.rollback")
    shutil.copy2(target, staged)
    os.replace(previous, target)
    os.replace(staged, previous)
    # Keep the host-key recovery copy equal to the active version.
    recovery = store / "recovery"
    rescue = _sealed(name, decrypt(name, store), recovery, "host")
    os.replace(rescue, recovery / target.name)


@contextlib.contextmanager
def materialized(
    names: tuple[str, ...], store: Path = STORE
) -> Iterator[dict[str, Path]]:
    """Decrypt into a private tmpfs directory for one command, then delete."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime or not Path(runtime).is_dir():
        raise SecretError("XDG_RUNTIME_DIR (tmpfs) is required for plaintext")
    directory = Path(tempfile.mkdtemp(prefix="klukai-secrets-", dir=runtime))
    try:
        os.chmod(directory, 0o700)
        paths = {}
        for name in names:
            path = directory / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(decrypt(name, store))
            paths[name] = path
        yield paths
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def check(store: Path = STORE, plaintext: tuple[Path, ...] = ()) -> dict[str, object]:
    _store(store)
    sizes = {name: len(decrypt(name, store)) for name in NAMES}
    for name in NAMES:
        rescue = store / "recovery" / f"{name}.cred"
        _check_private(rescue, 0o600)
        if _creds("decrypt", f"--name=klukai-{name}", str(rescue), "-") != decrypt(
            name, store
        ):
            raise SecretError(f"recovery copy of {name} differs from the primary")
    leftovers = [str(p) for p in plaintext if p.exists()]
    if leftovers:
        raise SecretError(f"plaintext copies still present: {leftovers}")
    return {"store": str(store), "credentials": sizes, "plaintext_absent": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=STORE)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("migrate", "rotate"):
        p = sub.add_parser(command)
        p.add_argument("name", choices=NAMES)
        p.add_argument("source", type=Path)
        p.add_argument(
            "--remove-source",
            action="store_true",
            help="delete the plaintext source after a verified encryption",
        )
    r = sub.add_parser("rollback")
    r.add_argument("name", choices=NAMES)
    v = sub.add_parser("recover")
    v.add_argument("name", choices=NAMES)
    c = sub.add_parser("check")
    c.add_argument("--plaintext", type=Path, action="append", default=[])
    args = parser.parse_args()
    try:
        if args.command in ("migrate", "rotate"):
            data = args.source.read_bytes()
            result = encrypt(args.name, data, args.store)
            if decrypt(args.name, args.store) != data:
                raise SecretError("stored credential differs from the source")
            if args.remove_source:
                args.source.unlink()
                result["source_removed"] = True
            print(result)
        elif args.command == "rollback":
            rollback(args.name, args.store)
            print({"name": args.name, "rolled_back": True})
        elif args.command == "recover":
            print(recover(args.name, args.store))
        else:
            print(check(args.store, tuple(args.plaintext)))
    except (SecretError, OSError) as error:
        print(f"klukai-secrets refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
