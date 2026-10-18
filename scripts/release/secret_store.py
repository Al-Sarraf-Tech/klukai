"""Consume user-scoped systemd encrypted credentials without shell expansion.

The user unit supplies LoadCredentialEncrypted=klukai-runtime:... and a private
RuntimeDirectory. Only the dedicated metrics token is materialized. Compose's
interpolation variables stay in the child environment, preserving exact values.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import Mapping, TypedDict

CREDENTIAL_ID = "klukai-runtime"
RESERVED = frozenset(
    {
        "PATH",
        "HOME",
        "SHELL",
        "ENV",
        "BASH_ENV",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "COMPOSE_FILE",
        "COMPOSE_PROJECT_NAME",
        "CREDENTIALS_DIRECTORY",
        "RUNTIME_DIRECTORY",
        "XDG_RUNTIME_DIR",
        "KLUKAI_RUNTIME_SECRETS_DIR",
        "GIT_ASKPASS",
        "GIT_SSH_COMMAND",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    }
)


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")


class CredentialError(ValueError):
    """A safe, non-secret-bearing validation failure."""


class Payload(TypedDict):
    schema_version: int
    environment: dict[str, str]
    metrics_token: str


def validate(data: object) -> Payload:
    if not isinstance(data, dict) or set(data) != {
        "schema_version",
        "environment",
        "metrics_token",
    }:
        raise CredentialError("invalid credential schema")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise CredentialError("unsupported credential schema")
    environment = data["environment"]
    if not isinstance(environment, dict) or not environment:
        raise CredentialError("missing credential environment")
    for name, value in environment.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise CredentialError("invalid environment name")
        if name in RESERVED or name.startswith(
            ("LD_", "DYLD_", "PYTHON", "SUDO_", "RUNNER_")
        ):
            raise CredentialError("reserved tool environment")
        if not isinstance(value, str) or "\0" in value:
            raise CredentialError("invalid environment value")
    token = data["metrics_token"]
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise CredentialError("invalid metrics credential")
    return {
        "schema_version": 1,
        "environment": dict(environment),
        "metrics_token": token,
    }


def load_credential(path: Path, *, uid: int | None = None) -> Payload:
    expected_uid = os.getuid() if uid is None else uid
    if not path.is_absolute():
        raise CredentialError("credential path must be absolute")
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        )
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise CredentialError("credential must be regular")
            if info.st_uid != expected_uid:
                raise CredentialError("credential owner mismatch")
            if info.st_mode & 0o077:
                raise CredentialError("credential must be private")
            raw = source.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise CredentialError("credential exceeds size bound")
        data = json.loads(raw)
    except (OSError, ValueError) as error:
        if isinstance(error, CredentialError):
            raise
        raise CredentialError("credential cannot be loaded") from None
    return validate(data)


def merge_environment(payload: Payload, base: Mapping[str, str]) -> dict[str, str]:
    result = dict(base)
    result.update(validate(payload)["environment"])
    return result


def install_metrics_token(payload: Payload, runtime: Path) -> Path:
    return install_token(validate(payload)["metrics_token"], runtime, "metrics-token")


def install_token(token: str, runtime: Path, name: str) -> Path:
    """Atomically write one bearer token, mode 0400, into a private directory.

    The directory is kept and only the file is replaced, so a container that
    bind-mounts the directory sees a rotated token without being recreated.
    """
    if not TOKEN_PATTERN.fullmatch(token):
        raise CredentialError(f"invalid {name} credential")
    if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
        raise CredentialError("invalid token file name")
    try:
        info = runtime.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise CredentialError("runtime directory must be private and owned")
        temporary = runtime / (f".{name}-" + secrets.token_hex(12))
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        try:
            with os.fdopen(descriptor, "w") as output:
                output.write(token)
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), 0o400)
            target = runtime / name
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError:
        raise CredentialError(f"{name} credential cannot be materialized") from None
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate without materializing or executing",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        directory = os.environ.get("CREDENTIALS_DIRECTORY")
        if not directory:
            raise CredentialError("credential directory missing")
        payload = load_credential(Path(directory) / CREDENTIAL_ID)
        if args.check:
            print("credential validated")
            return 0
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command or not Path(command[0]).is_absolute():
            raise CredentialError("absolute executable required")
        runtime = os.environ.get("RUNTIME_DIRECTORY")
        if not runtime:
            raise CredentialError("runtime directory missing")
        install_metrics_token(payload, Path(runtime))
        environment = merge_environment(payload, os.environ)
        environment["KLUKAI_RUNTIME_SECRETS_DIR"] = runtime
        os.execve(command[0], command, environment)
    except (CredentialError, OSError):
        print("klukai credential validation or execution failed", file=sys.stderr)
        return 1
    return 1  # execve only returns by raising.


if __name__ == "__main__":
    raise SystemExit(main())
