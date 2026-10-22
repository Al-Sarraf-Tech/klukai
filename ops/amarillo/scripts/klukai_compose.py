#!/usr/bin/env python3
"""docker compose for Klukai's production stack, secrets from the encrypted store.

    python3 ops/amarillo/scripts/klukai_compose.py up -d --no-build --no-deps events-bridge

Equivalent to `docker compose` in ~/git/klukai, except that the environment
file and the private companion-core override are decrypted from the
systemd-creds store (klukai_secrets.py) into a private tmpfs directory for this
one command, the web-root override (once the web release manager is
activated, klukai_web_release.py) moves /app/static to the atomic web root, and
the release deployer's image pin is applied last. Plain
`docker compose` no longer has the secrets and must not be used for this stack.
Note that `config` prints resolved values, secrets included.

    python3 ops/amarillo/scripts/klukai_compose.py --obs up -d --no-deps prometheus

`--obs` runs the observability project (`-p klukai`, docker-compose.obs.yml)
instead. It needs no store secrets: its two bearer tokens are mounted from
/run/klukai-secrets, written at boot by klukai-runtime-secrets.service. An empty
environment file keeps a stray `.env` from changing those mount paths.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import klukai_secrets  # noqa: E402

REPO = HERE.parents[2]
# The deploy-on-merge controller runs this code from a pinned, owner-installed
# copy and points it at the production checkout, whose compose file and
# relative mounts (./config, ./assets/build) are what production serves.
PRODUCTION = Path(os.environ.get("KLUKAI_PRODUCTION_CHECKOUT") or REPO)
OBS_PROJECT = "klukai"
PIN = Path.home() / ".local/state/klukai/release/core-image.json"
WEB_ROOT = Path.home() / ".local/state/klukai/release/web-root.json"


def argv(
    paths: dict[str, Path],
    args: list[str],
    pin: Path = PIN,
    web_root: Path = WEB_ROOT,
) -> list[str]:
    files = [
        "-f",
        str(PRODUCTION / "docker-compose.yml"),
        "-f",
        str(paths["core-override"]),
    ]
    if web_root.is_file():
        files += ["-f", str(web_root)]
    if pin.is_file():
        files += ["-f", str(pin)]
    return ["docker", "compose", "--env-file", str(paths["compose-env"]), *files, *args]


def obs_argv(args: list[str]) -> list[str]:
    return [
        "docker",
        "compose",
        "-p",
        OBS_PROJECT,
        "--env-file",
        os.devnull,
        "-f",
        str(PRODUCTION / "docker-compose.obs.yml"),
        *args,
    ]


def main() -> int:
    if sys.argv[1:2] == ["--obs"]:
        return subprocess.run(obs_argv(sys.argv[2:]), cwd=PRODUCTION).returncode
    # SIGTERM becomes SystemExit so the tmpfs plaintext is still removed.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(143))
    try:
        with klukai_secrets.materialized(klukai_secrets.COMPOSE_NAMES) as paths:
            return subprocess.run(argv(paths, sys.argv[1:]), cwd=PRODUCTION).returncode
    except klukai_secrets.SecretError as error:
        print(f"klukai-compose refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
