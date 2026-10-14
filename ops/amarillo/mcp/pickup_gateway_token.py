#!/usr/bin/env python3
"""Publish only Klukai's newest gateway key drop; never print credentials."""

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import os
from pathlib import Path
import re
import secrets
import stat
import sys


DROP_NAME = re.compile(r"klukai-web-tools_([0-9]{8}T[0-9]{6}Z)\.txt")
TOKEN = re.compile(rb"mk_[a-z0-9]{12}_[A-Za-z0-9_-]{43}\n?")
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@contextmanager
def directory(path, *, create_leaf=False):
    """Open each path component without following symlinks, including ancestors."""
    components = Path(os.path.abspath(path)).parts[1:]
    descriptor = os.open("/", DIRECTORY_FLAGS)
    try:
        for index, component in enumerate(components):
            if create_leaf and index == len(components) - 1:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(component, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def newest_drop(source_fd):
    """Inspect names only; never stat or open another client's drop."""
    newest = None
    for name in os.listdir(source_fd):
        match = DROP_NAME.fullmatch(name)
        if match is None:
            continue
        try:
            datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
        except ValueError:
            continue
        if newest is None or name > newest:
            newest = name
    return newest


def regular_file(file_stat):
    # Multiple hard links also make a credential's provenance ambiguous.
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
        raise ValueError("unsafe credential file")


def read_drop(source_fd, name):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    descriptor = os.open(name, flags, dir_fd=source_fd)
    try:
        before = os.fstat(descriptor)
        regular_file(before)
        # A key is 59 bytes, with at most one newline. Never read an unbounded file.
        token = os.read(descriptor, 61)
        after = os.fstat(descriptor)
        if (before.st_mtime_ns, before.st_ctime_ns, before.st_size) != (
            after.st_mtime_ns, after.st_ctime_ns, after.st_size
        ) or TOKEN.fullmatch(token) is None:
            raise ValueError("invalid or changing credential file")
        return token.rstrip(b"\n") + b"\n", after
    finally:
        os.close(descriptor)


def current_destination(destination_fd, name):
    try:
        current = os.stat(name, dir_fd=destination_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    regular_file(current)
    return current


def publish(destination_fd, name, token, source_stat, owner_uid, owner_gid):
    temporary = "." + name + "." + secrets.token_hex(16)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(temporary, flags, 0o600, dir_fd=destination_fd)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchown(handle.fileno(), owner_uid, owner_gid)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(token)
            handle.flush()
            os.utime(handle.fileno(), ns=(source_stat.st_mtime_ns, source_stat.st_mtime_ns))
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=destination_fd, dst_dir_fd=destination_fd)
        os.fsync(destination_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=destination_fd)
        except FileNotFoundError:
            pass


def pickup(source, destination, owner_uid, owner_gid):
    with directory(source) as source_fd:
        name = newest_drop(source_fd)
        if name is None:
            return
        token, source_stat = read_drop(source_fd, name)
    destination = Path(destination)
    with directory(destination.parent, create_leaf=True) as destination_fd:
        # Serialize manual invocations as well as the systemd oneshot.
        fcntl.flock(destination_fd, fcntl.LOCK_EX)
        os.fchown(destination_fd, owner_uid, owner_gid)
        os.fchmod(destination_fd, 0o700)
        current = current_destination(destination_fd, destination.name)
        if current is not None and source_stat.st_mtime_ns <= current.st_mtime_ns:
            return
        publish(destination_fd, destination.name, token, source_stat, owner_uid, owner_gid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", default="/docker/mcp/gateway/key_drops")
    parser.add_argument(
        "--destination",
        default="/home/jalsarraf/.local/share/mcp-stack/klukai/gateway.token",
    )
    parser.add_argument("--owner-uid", type=int, default=1000)
    parser.add_argument("--owner-gid", type=int, default=1000)
    options = parser.parse_args()
    try:
        if options.owner_uid < 0 or options.owner_gid < 0:
            raise ValueError("invalid owner")
        pickup(options.source_dir, options.destination, options.owner_uid, options.owner_gid)
    except (OSError, ValueError, OverflowError):
        # Do not expose filenames, exception payloads, or credential text to journald.
        print("Klukai gateway credential pickup failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
