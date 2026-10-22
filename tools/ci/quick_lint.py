"""Fail formatting quickly using only Ruff from the complete committed CI lock."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys

from .light_environment import public_downloads
from .tool_materials import validate_hash_lock


def ruff_lock(text: str) -> str:
    match = re.search(r"^ruff==[^\n]*(?:\n[ \t]+[^\n]*)*", text, re.MULTILINE)
    if match is None:
        raise ValueError("Missing pinned Ruff closure")
    selected = match.group() + "\n"
    packages = validate_hash_lock(selected, 1)
    if set(packages) != {"ruff"}:
        raise ValueError("Unexpected preflight closure")
    return selected


def run(root: Path, output: Path) -> None:
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    lock = output / "ruff.lock"
    lock.write_text(ruff_lock((root / "docker/core/requirements-ci.txt").read_text()))
    environment = output / "environment"
    subprocess.run([sys.executable, "-I", "-m", "venv", str(environment)], check=True)
    python = str(environment / "bin/python")
    subprocess.run(
        [
            python,
            "-I",
            "-m",
            "pip",
            "--isolated",
            "install",
            "--no-deps",
            "--require-hashes",
            "--no-compile",
            *public_downloads(),
            "-r",
            str(lock),
        ],
        check=True,
    )
    for command in [("check", "app/"), ("format", "--check", "app/")]:
        subprocess.run(
            [python, "-I", "-m", "ruff", *command, "--config", "ruff.toml"],
            cwd=root / "docker/core",
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.root, args.output)


if __name__ == "__main__":
    main()
