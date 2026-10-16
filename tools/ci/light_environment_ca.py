"""Validate and bind only the public CA bundle selected by trusted builder pip."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import ssl
import stat
from typing import Any, Protocol

from .core_source import require, sha256

PROBE = """import json
from pip._vendor import certifi
print(json.dumps({'klukai_builder_ca':certifi.where()}))
"""
SYSTEM_ROOTS = tuple(
    map(Path, ("/etc/pki", "/etc/ssl", "/usr/share", "/usr/local/share"))
)
CERTIFICATE = re.compile(
    r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\r\n \t]+-----END CERTIFICATE-----"
)


class Runner(Protocol):
    def run(self, argv: list[str]) -> str: ...


def policy(path: Path, builder: Path) -> tuple[str, Path]:
    require(
        builder.is_absolute() and builder == builder.resolve() and builder.is_dir(),
        "CA requires the exact existing trusted builder",
    )
    require(
        path.is_absolute()
        and ".." not in path.parts
        and not any(char in str(path) for char in "\x00\n\r"),
        "Unsafe public CA path",
    )
    internal = path.is_relative_to(builder)
    if not internal:
        require(
            any(path.is_relative_to(root) for root in SYSTEM_ROOTS),
            "CA path is outside system trust layouts",
        )
    resolved = path.resolve(strict=True)
    if internal:
        require(
            resolved.is_relative_to(builder) and not path.is_symlink(),
            "Bundled CA link escapes/aliases a file",
        )
        owner, kind = os.getuid(), "bundled"
        parents = tuple(
            parent for parent in path.parents if parent.is_relative_to(builder)
        )
    else:
        require(
            any(resolved.is_relative_to(root) for root in SYSTEM_ROOTS),
            "System CA link escapes trust layouts",
        )
        owner, kind, parents = 0, "system", tuple(path.parents)
    for item in (path, *parents, resolved, *(resolved.parents if not internal else ())):
        require(
            item.stat().st_uid == owner
            and item.lstat().st_uid == owner
            and item.stat().st_mode & 0o022 == 0,
            "Public CA material has unsafe owner/permissions",
        )
    require(
        stat.S_ISREG(resolved.lstat().st_mode) and resolved.stat().st_nlink == 1,
        "Public CA must resolve to a regular nonlinked file",
    )
    return kind, resolved


def certificate_count(content: bytes) -> int:
    text = content.decode("utf-8")
    blocks = CERTIFICATE.findall(text)
    remainder = CERTIFICATE.sub("", text)
    require(
        blocks
        and all(
            not line.strip() or line.lstrip().startswith("#")
            for line in remainder.splitlines()
        ),
        "CA bundle contains nonpublic/noncertificate material",
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata="\n".join(blocks))
    stats = context.cert_store_stats()
    require(
        stats["x509_ca"] > 0 and stats["x509_ca"] == stats["x509"],
        "Bundle contains non-CA certificates",
    )
    return stats["x509_ca"]


def file_identity(info: os.stat_result) -> list[int]:
    return [
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_gid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    ]


def inspect(path: Path, builder: Path) -> dict[str, Any]:
    kind, resolved = policy(path, builder)
    before = resolved.stat()
    require(0 < before.st_size <= 16 * 1024**2, "Invalid public CA bundle size")
    certificates = certificate_count(resolved.read_bytes())
    record = {
        "builder": str(builder),
        "requested": str(path),
        "resolved": str(resolved),
        "kind": kind,
        "sha256": sha256(resolved),
        "size": before.st_size,
        "certificates": certificates,
        "identity": file_identity(before),
    }
    after = resolved.stat()
    require(
        file_identity(before) == file_identity(after),
        "Public CA material changed during inspection",
    )
    return record


def unchanged(record: Any, builder: Path) -> None:
    require(isinstance(record, dict), "Missing/malformed public CA identity")
    try:
        require(
            inspect(Path(record["requested"]), builder) == record,
            "Public CA identity or bytes changed",
        )
    except (KeyError, TypeError) as error:
        raise ValueError("Malformed public CA identity") from error


def discover(runner: Runner, builder: Path) -> dict[str, Any]:
    result = json.loads(
        runner.run([str(builder / "bin/python"), "-I", "-B", "-c", PROBE])
    )
    require(
        isinstance(result, dict) and isinstance(result.get("klukai_builder_ca"), str),
        "Trusted builder returned no public CA path",
    )
    return inspect(Path(result["klukai_builder_ca"]), builder)


def mounts(record: dict[str, Any], builder: Path) -> list[tuple[Path, Path]]:
    unchanged(record, builder)
    if record["kind"] == "bundled":
        return []  # Already covered by the exact builder's read-only mount.
    requested, resolved = Path(record["requested"]), Path(record["resolved"])
    target = requested if not requested.is_relative_to("/usr") else resolved
    return [] if target.is_relative_to("/usr") else [(resolved, target)]
