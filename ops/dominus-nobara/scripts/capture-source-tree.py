#!/usr/bin/env python3
"""Capture or verify the complete Git-allowlisted recovery source tree."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys


_TEMPLATE_MARKER = re.compile(r"(?:^|[._-])(example|sample|template)(?:$|[._-])")
_CREDENTIAL_MARKER = re.compile(
    r"(?:^|[._-])(credential|credentials|secret|secrets|token|tokens)(?:$|[._-])"
)
_PEM_PRIVATE_KEY = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"
)
_TELEGRAM_BOT_TOKEN = re.compile(
    rb"(?<![A-Za-z0-9])\d{8,12}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"
)
_SOURCE_SUFFIXES = frozenset(
    {".c", ".cc", ".go", ".h", ".java", ".js", ".md", ".py", ".rs", ".sh", ".ts"}
)


def git_allowlist(source_root: Path) -> bytes:
    process = subprocess.run(
        [
            "git",
            "-C",
            str(source_root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if process.returncode != 0:
        raise RuntimeError("git could not produce the source allowlist")
    entries = sorted(item for item in process.stdout.split(b"\0") if item)
    if not entries:
        raise RuntimeError("Git source allowlist is empty")
    if len(entries) != len(set(entries)):
        raise RuntimeError("Git source allowlist contains duplicate paths")
    return b"\0".join(entries) + b"\0"


def _safe_path(source_root: Path, raw_path: bytes) -> tuple[str, Path]:
    path_text = os.fsdecode(raw_path)
    relative = PurePosixPath(path_text)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise RuntimeError(f"unsafe source allowlist path: {path_text!r}")
    full_path = source_root.joinpath(*relative.parts)
    try:
        full_path.parent.resolve(strict=True).relative_to(source_root)
    except (OSError, ValueError) as error:
        raise RuntimeError(f"source path escapes the recovery tree: {path_text!r}") from error
    return path_text, full_path


def reject_sensitive_allowlist_paths(raw_entries: list[bytes]) -> None:
    """Fail closed if Git ever tracks deployment credentials or key material."""

    for raw_path in raw_entries:
        path_text = os.fsdecode(raw_path)
        relative = PurePosixPath(path_text)
        basename = relative.name.lower()
        template = _TEMPLATE_MARKER.search(basename) is not None
        env_variant = (
            basename == ".env"
            or basename.startswith(".env.")
            or basename.endswith(".env")
            or ".env." in basename
        )
        private_extension = relative.suffix.lower() in {
            ".cer",
            ".crt",
            ".key",
            ".p12",
            ".pem",
            ".pfx",
        }
        credential_data = (
            _CREDENTIAL_MARKER.search(basename) is not None
            and relative.suffix.lower() not in _SOURCE_SUFFIXES
        )
        sensitive_directory = any(
            part.lower() in {".ssh", "credentials", "private", "secrets"}
            for part in relative.parts[:-1]
        )
        if not template and (
            env_variant
            or private_extension
            or credential_data
            or sensitive_directory
        ):
            raise RuntimeError(
                "source allowlist contains a denied sensitive deployment path"
            )


def reject_high_confidence_secret_content(payload: bytes) -> None:
    """Reject unmistakable credential shapes without echoing data or paths."""

    if _PEM_PRIVATE_KEY.search(payload) or _TELEGRAM_BOT_TOKEN.search(payload):
        raise RuntimeError("source allowlist contains denied sensitive content")


def build_manifest(source_root: Path, allowlist: bytes) -> bytes:
    source_root = source_root.resolve(strict=True)
    raw_entries = [item for item in allowlist.split(b"\0") if item]
    if not raw_entries or raw_entries != sorted(raw_entries):
        raise RuntimeError("source allowlist must be non-empty and byte-sorted")
    if len(raw_entries) != len(set(raw_entries)):
        raise RuntimeError("source allowlist contains duplicate paths")
    reject_sensitive_allowlist_paths(raw_entries)

    lines: list[bytes] = []
    for raw_path in raw_entries:
        path_text, full_path = _safe_path(source_root, raw_path)
        metadata = full_path.lstat()
        if stat.S_ISREG(metadata.st_mode):
            payload = full_path.read_bytes()
            reject_high_confidence_secret_content(payload)
            entry_type = "file"
        elif stat.S_ISLNK(metadata.st_mode):
            resolved = full_path.resolve(strict=True)
            try:
                resolved.relative_to(source_root)
            except ValueError as error:
                raise RuntimeError(
                    f"source symlink escapes the recovery tree: {path_text!r}"
                ) from error
            payload = os.fsencode(os.readlink(full_path))
            entry_type = "symlink"
        else:
            raise RuntimeError(f"unsupported allowlisted source type: {path_text!r}")
        document = {
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
            "path": path_text,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
            "type": entry_type,
        }
        lines.append(
            json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
            .encode("utf-8")
            + b"\n"
        )
    return b"".join(lines)


def actual_source_entries(source_root: Path) -> bytes:
    """Return every regular file or symlink below a staged source root."""

    source_root = source_root.resolve(strict=True)
    root_bytes = os.fsencode(source_root)
    entries: list[bytes] = []
    for current, directories, filenames in os.walk(
        root_bytes, topdown=True, followlinks=False
    ):
        for directory in list(directories):
            full_path = os.path.join(current, directory)
            metadata = os.lstat(full_path)
            if stat.S_ISLNK(metadata.st_mode):
                directories.remove(directory)
                entries.append(os.path.relpath(full_path, root_bytes))
            elif not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("staged source contains an unsupported entry type")
        for filename in filenames:
            full_path = os.path.join(current, filename)
            metadata = os.lstat(full_path)
            if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
                raise RuntimeError("staged source contains an unsupported entry type")
            entries.append(os.path.relpath(full_path, root_bytes))
    entries.sort()
    if len(entries) != len(set(entries)):
        raise RuntimeError("staged source contains duplicate paths")
    return b"\0".join(entries) + (b"\0" if entries else b"")


def verify_exact_set(source_root: Path, allowlist: bytes) -> None:
    """Reject stale or injected files outside the captured deployment set."""

    actual = actual_source_entries(source_root)
    if not hmac.compare_digest(actual, allowlist):
        expected_entries = {item for item in allowlist.split(b"\0") if item}
        actual_entries = {item for item in actual.split(b"\0") if item}
        raise RuntimeError(
            "staged source file set differs from the deployment allowlist "
            f"(missing={len(expected_entries - actual_entries)}, "
            f"extra={len(actual_entries - expected_entries)})"
        )


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--allowlist", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output-dir", type=Path)
    mode.add_argument("--verify-manifest", type=Path)
    parser.add_argument(
        "--require-exact-set",
        action="store_true",
        help="reject files outside the allowlist while verifying a fresh stage",
    )
    arguments = parser.parse_args()

    if arguments.require_exact_set and (
        arguments.verify_manifest is None or arguments.allowlist is None
    ):
        parser.error("--require-exact-set requires --verify-manifest and --allowlist")

    try:
        source_root = arguments.source_root.resolve(strict=True)
        allowlist = (
            arguments.allowlist.read_bytes()
            if arguments.allowlist is not None
            else git_allowlist(source_root)
        )
        manifest = build_manifest(source_root, allowlist)
        tree_digest = hashlib.sha256(manifest).hexdigest()
        if arguments.verify_manifest is not None:
            expected = arguments.verify_manifest.read_bytes()
            if not hmac.compare_digest(manifest, expected):
                raise RuntimeError("source tree differs from the captured manifest")
            if arguments.require_exact_set:
                verify_exact_set(source_root, allowlist)
        else:
            assert arguments.output_dir is not None
            output_dir = arguments.output_dir.resolve(strict=False)
            if _inside(output_dir, source_root):
                raise RuntimeError("source snapshot output must be outside the source tree")
            output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
            (output_dir / "source-files.nul").write_bytes(allowlist)
            (output_dir / "source-files.jsonl").write_bytes(manifest)
            (output_dir / "source-tree.sha256").write_text(
                f"{tree_digest}  source-files.jsonl\n", encoding="utf-8"
            )
        print(
            json.dumps(
                {
                    "files": manifest.count(b"\n"),
                    "source_tree_sha256": tree_digest,
                    "status": "verified"
                    if arguments.verify_manifest is not None
                    else "captured",
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"status": "rejected", "error": str(error)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
