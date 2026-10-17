#!/usr/bin/env python3
"""docker compose for Klukai's production stack, secrets from the encrypted store.

    python3 ops/amarillo/scripts/klukai_compose.py up -d --no-build --no-deps events-bridge

Equivalent to `docker compose` in ~/git/klukai, except that the environment
file and the private companion-core override are decrypted from the
systemd-creds store (klukai_secrets.py) into a private tmpfs directory for this
one command, and the release deployer's image pin is applied last. Plain
`docker compose` no longer has the secrets and must not be used for this stack.
Note that `config` prints resolved values, secrets included.
"""

from __future__ import annotations

from pathlib import Path
import signal
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import klukai_secrets  # noqa: E402

REPO = HERE.parents[2]
PIN = Path.home() / ".local/state/klukai/release/core-image.json"


def argv(paths: dict[str, Path], args: list[str], pin: Path = PIN) -> list[str]:
    files = ["-f", str(REPO / "docker-compose.yml"), "-f", str(paths["core-override"])]
    if pin.is_file():
        files += ["-f", str(pin)]
    return ["docker", "compose", "--env-file", str(paths["compose-env"]), *files, *args]


def main() -> int:
    # SIGTERM becomes SystemExit so the tmpfs plaintext is still removed.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(143))
    try:
        with klukai_secrets.materialized(klukai_secrets.NAMES) as paths:
            return subprocess.run(argv(paths, sys.argv[1:]), cwd=REPO).returncode
    except klukai_secrets.SecretError as error:
        print(f"klukai-compose refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
