#!/usr/bin/env python3
"""Boot-time copies of Klukai's two internal bearer tokens, from the encrypted store.

Run by the system unit klukai-runtime-secrets.service, as the owner's user and
ordered Before=docker.service, so the tokens exist before Docker restarts
companion-core, Prometheus and Alertmanager at boot (no user session exists yet):

    /run/klukai-secrets/metrics/metrics-token             core + Prometheus
    /run/klukai-secrets/alert-bridge/alert-bridge-token   Alertmanager + host bridge

Each token is decrypted from the systemd-creds store (klukai_secrets.py,
~/.config/klukai/credentials) and written 0400 into its consumers' own 0700
directory, which systemd creates on /run (tmpfs) as RuntimeDirectory=. Neither
consumer can read the other token. The directories survive a stop
(RuntimeDirectoryPreserve=yes), so bind mounts stay valid and a rotated token
is visible without recreating anything; `remove` deletes only the files, so a
stopped unit leaves the consumers with a missing token, never an empty one.

    klukai_runtime_secrets.py install   # decrypt both, then write both (ExecStart)
    klukai_runtime_secrets.py remove    # delete both files (ExecStop)
    klukai_runtime_secrets.py check     # present, private, equal to the store

Values are never printed. Comparisons are made in memory.
"""

from __future__ import annotations

import argparse
import hmac
import os
from pathlib import Path
import stat
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2] / "scripts" / "release"))
import klukai_secrets  # noqa: E402
import secret_store  # noqa: E402

RUNTIME = Path("/run/klukai-secrets")
# Credential name -> the consumers' directory under RUNTIME.
LAYOUT = {"metrics-token": "metrics", "alert-bridge-token": "alert-bridge"}
assert tuple(LAYOUT) == klukai_secrets.TOKEN_NAMES


def token(name: str, store: Path = klukai_secrets.STORE) -> str:
    raw = klukai_secrets.decrypt(name, store)
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        value = ""
    if not secret_store.TOKEN_PATTERN.fullmatch(value):
        raise klukai_secrets.SecretError(f"credential {name} is not a valid token")
    return value


def install(
    runtime: Path = RUNTIME, store: Path = klukai_secrets.STORE
) -> dict[str, object]:
    # Decrypt everything before writing anything: a bad credential changes nothing.
    values = {name: token(name, store) for name in LAYOUT}
    for name, directory in LAYOUT.items():
        secret_store.install_token(values[name], runtime / directory, name)
    return {"installed": list(LAYOUT), "runtime": str(runtime)}


def remove(runtime: Path = RUNTIME) -> dict[str, object]:
    removed = []
    for name, directory in LAYOUT.items():
        folder = runtime / directory
        if not folder.is_dir() or folder.is_symlink():
            continue
        for path in [folder / name, *folder.glob(f".{name}-*")]:
            if path.is_file() and not path.is_symlink():
                path.unlink()
                removed.append(path.name)
    return {"removed": removed}


def check(
    runtime: Path = RUNTIME, store: Path = klukai_secrets.STORE
) -> dict[str, object]:
    for name, directory in LAYOUT.items():
        folder = runtime / directory
        klukai_secrets._check_private(folder, 0o700)
        path = folder / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise klukai_secrets.SecretError(f"{path} is not a regular file")
        klukai_secrets._check_private(path, 0o400)
        if not hmac.compare_digest(path.read_bytes(), token(name, store).encode()):
            raise klukai_secrets.SecretError(f"{path} differs from the store")
    return {"tokens": list(LAYOUT), "private": True, "match_store": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "remove", "check"))
    parser.add_argument("--runtime", type=Path, default=RUNTIME)
    parser.add_argument("--store", type=Path, default=klukai_secrets.STORE)
    args = parser.parse_args()
    try:
        if args.command == "install":
            print(install(args.runtime, args.store))
        elif args.command == "remove":
            print(remove(args.runtime))
        else:
            print(check(args.runtime, args.store))
    except (klukai_secrets.SecretError, secret_store.CredentialError, OSError) as error:
        print(f"klukai-runtime-secrets refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
