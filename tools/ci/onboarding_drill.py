"""Run docs/onboarding.md §2-§4 from a clean isolated checkout (DOC-1 drill).

The drill clones the exact commit into an empty directory, provides the
prerequisite the doc names ("equivalent containers on those networks") from
pinned throwaway images, fills the required `.env` values with random
throwaway secrets (the doc's manual edit), then runs the doc's commands. Every
command must still appear verbatim in the doc, so the drill fails when the doc
drifts from what actually works. The result records what ran, what passed and
what was not exercised. Everything it creates is removed afterwards.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

NETWORKS = ("aichat_default", "infra", "klukai-obs")
BUILT_SERVICES = ("companion-core", "events-bridge")


def built_images(checkout: Path) -> list[str]:
    """Images compose builds for this checkout: `<project>-<service>`, where the
    project is the compose file's `name:` (Klukai pins `companion`)."""
    text = (checkout / "docker-compose.yml").read_text()
    found = re.search(r"^name:\s*([\w.-]+)\s*$", text, re.M)
    project = found.group(1) if found else checkout.name
    return [f"{project}-{service}" for service in BUILT_SERVICES]


INFRA = {
    "infra-postgres": "KLUKAI_PERF_POSTGRES_IMAGE",
    "aichat-redis": "KLUKAI_PERF_REDIS_IMAGE",
    "aichat-vector": "KLUKAI_PERF_QDRANT_IMAGE",
}
LOGIN = (
    "TOKEN=$(curl -s -X POST http://127.0.0.1:8300/api/auth/login \\\n"
    "  -H 'Content-Type: application/json' \\\n"
    '  -d \'{"username":"jalsarraf","password":"\'"${SEED_PASSWORD_JALSARRAF}"\'"}\''
    " | jq -r .token)\n"
    'curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8300/api/session/info | jq .'
)
STEPS = (
    ("2", "test -e .env || cp .env.example .env"),
    ("2", "chmod 600 .env"),
    ("fill", ""),  # the doc's manual edit of the required values
    ("2", "docker compose config --quiet"),
    ("3", "docker network ls | grep -E 'aichat_default|infra|klukai-obs'"),
    ("3", "docker compose up -d companion-core"),
    ("wait", ""),  # the doc expects "Up (healthy)" before the next commands
    ("3", "docker compose ps"),
    ("3", "curl -s http://127.0.0.1:8300/health | jq ."),
    ("4", LOGIN),
)
NOT_EXERCISED = [
    "§1 clone over SSH from GitHub: the drill clones the same commit from the CI checkout",
    "§3 optional observability stack (docker-compose.obs.yml binds amarillo paths)",
    "§5 chat: needs the Dominus GPU services",
    "§6 tests and §7 audit: run by the CI jobs themselves",
]


def sh(
    command: str, cwd: Path, env: dict[str, str], timeout: int = 900
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-o", "pipefail", "-c", command],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def documented(doc: str, command: str) -> bool:
    """The command (ignoring trailing # comments) appears verbatim in the doc."""
    lines = [re.sub(r"\s+#.*$", "", line).rstrip() for line in doc.splitlines()]
    text = "\n".join(lines)
    return all(part.rstrip() in text for part in command.split("\n"))


def fill_env(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text().splitlines()
    seen = set()
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in values:
            lines[index] = f"{key}={values[key]}"
            seen.add(key)
    lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
    path.write_text("\n".join(lines) + "\n")


def provide_infrastructure(password: str) -> None:
    for network in NETWORKS:
        subprocess.run(["docker", "network", "create", network], capture_output=True)
    env = {
        "infra-postgres": [
            "-e",
            "POSTGRES_USER=aichat",
            "-e",
            "POSTGRES_DB=aichat",
            "-e",
            f"POSTGRES_PASSWORD={password}",
        ],
        "aichat-redis": [],
        "aichat-vector": [],
    }
    for name, variable in INFRA.items():
        image = os.environ[variable]
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--network",
                NETWORKS[1],
                *env[name],
                image,
            ],
            check=True,
            capture_output=True,
        )
        for network in (NETWORKS[0], NETWORKS[2]):
            subprocess.run(["docker", "network", "connect", network, name], check=True)
    for _ in range(90):
        ready = subprocess.run(
            [
                "docker",
                "exec",
                "infra-postgres",
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-U",
                "aichat",
            ],
            capture_output=True,
        )
        if ready.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("throwaway infra-postgres did not become ready")


def cleanup(checkout: Path | None) -> None:
    if checkout is not None and checkout.exists():
        subprocess.run(
            ["docker", "compose", "down", "-v", "--remove-orphans", "--rmi", "local"],
            cwd=checkout,
            capture_output=True,
            timeout=300,
        )
    subprocess.run(["docker", "rm", "-f", *INFRA], capture_output=True)
    for network in NETWORKS:
        subprocess.run(["docker", "network", "rm", network], capture_output=True)


def wait_healthy(timeout: float = 240) -> str:
    deadline = time.monotonic() + timeout
    state = "missing"
    while time.monotonic() < deadline:
        state = (
            subprocess.run(
                [
                    "docker",
                    "inspect",
                    "-f",
                    "{{.State.Health.Status}}",
                    "companion-core",
                ],
                capture_output=True,
                text=True,
            ).stdout.strip()
            or "missing"
        )
        if state == "healthy":
            break
        time.sleep(3)
    return state


def drill(source: Path, revision: str) -> dict[str, Any]:
    doc = (source / "docs/onboarding.md").read_text()
    for section, command in STEPS:
        if command and not documented(doc, command):
            raise RuntimeError(
                f"§{section} command is no longer in the doc: {command!r}"
            )
    work = Path(tempfile.mkdtemp(prefix="klukai-onboarding-"))
    checkout = work / "klukai"
    record: dict[str, Any] = {
        "date": datetime.now(UTC).date().isoformat(),
        "revision": revision,
        "executor": "nightly CI on the heavy runner (not the doc's author)",
        "environment": "clean clone of the commit; throwaway infra containers from pinned images",
        "steps": [],
        "not_exercised": NOT_EXERCISED,
    }
    secrets_env = {
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "LM_STUDIO_TOKEN": secrets.token_urlsafe(24),
        "VOICE_API_TOKEN": secrets.token_urlsafe(24),
        "RABBITMQ_PASSWORD": secrets.token_urlsafe(24),
        "SEED_PASSWORD_JALSARRAF": secrets.token_urlsafe(24),
    }
    env = {
        **os.environ,
        "SEED_PASSWORD_JALSARRAF": secrets_env["SEED_PASSWORD_JALSARRAF"],
    }
    try:
        # A developer's usual umask: the image copies source files with their
        # modes and runs as UID 1000, so a private (077) clone cannot start.
        previous_umask = os.umask(0o022)
        try:
            subprocess.run(
                ["git", "clone", "--quiet", "--no-local", str(source), str(checkout)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(checkout), "checkout", "--quiet", revision],
                check=True,
            )
        finally:
            os.umask(previous_umask)
        # A fresh machine has no locally built image: remove any left by an
        # earlier drill so `docker compose up` really builds from this clone.
        subprocess.run(
            ["docker", "image", "rm", "-f", *built_images(checkout)],
            capture_output=True,
        )
        provide_infrastructure(secrets_env["POSTGRES_PASSWORD"])
        for section, command in STEPS:
            step: dict[str, Any] = {"section": section, "command": command or section}
            if section == "fill":
                fill_env(checkout / ".env", secrets_env)
                step.update(
                    ok=True, note="required values set to random throwaway secrets"
                )
            elif section == "wait":
                state = wait_healthy()
                step.update(ok=state == "healthy", note=f"companion-core {state}")
            else:
                result = sh(command, checkout, env)
                output = result.stdout
                step.update(rc=result.returncode, ok=result.returncode == 0)
                if command.startswith("curl -s http://127.0.0.1:8300/health"):
                    health = json.loads(output)
                    step["ok"] = step["ok"] and health.get("status") == "ok"
                    step["health"] = {
                        k: health.get(k) for k in ("status", "redis", "qdrant")
                    }
                    step["health"]["database"] = (health.get("database") or {}).get(
                        "status"
                    )
                elif section == "4":
                    session = json.loads(output or "{}")
                    step["ok"] = step["ok"] and session.get("user_id") == "jalsarraf"
                    step["session_user"] = session.get("user_id")
                elif section == "3" and "compose ps" in command:
                    step["ok"] = step["ok"] and "healthy" in output
                if not step["ok"]:
                    step["stderr"] = result.stderr.strip()[-400:]
            record["steps"].append(step)
            if not step["ok"]:
                break
    finally:
        if not all(s.get("ok") for s in record["steps"]):
            probe = (
                "import os; print({p: (oct(os.stat(p).st_mode & 0o777), os.stat(p).st_uid)"
                " for p in ('/app', '/app/app', '/app/app/__init__.py')}, os.getuid())"
            )
            diagnostics = {
                "umask": oct(os.umask(0o022)),
                "clone_modes": sh(
                    "stat -c '%a %u %n' docker/core docker/core/app "
                    "docker/core/app/__init__.py",
                    checkout,
                    env,
                ).stdout,
                "image_modes": subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--entrypoint",
                        "/opt/venv/bin/python",
                        built_images(checkout)[0],
                        "-c",
                        probe,
                    ],
                    capture_output=True,
                    text=True,
                ).stdout
                + "",
                "image": subprocess.run(
                    [
                        "docker",
                        "image",
                        "inspect",
                        "-f",
                        "{{.Id}} {{.Created}} {{.Config.User}}",
                        built_images(checkout)[0],
                    ],
                    capture_output=True,
                    text=True,
                ).stdout,
                "container_user": subprocess.run(
                    [
                        "docker",
                        "inspect",
                        "-f",
                        "{{.Config.User}} {{.HostConfig.UsernsMode}} {{json .HostConfig.SecurityOpt}}",
                        "companion-core",
                    ],
                    capture_output=True,
                    text=True,
                ).stdout,
            }
            record["diagnostics"] = diagnostics
            logs = subprocess.run(
                ["docker", "logs", "--tail", "40", "companion-core"],
                capture_output=True,
                text=True,
            )
            record["core_log_tail"] = (logs.stdout + logs.stderr)[-3000:]
        cleanup(checkout)
        shutil.rmtree(work, ignore_errors=True)
    record["passed"] = len(record["steps"]) == len(STEPS) and all(
        s["ok"] for s in record["steps"]
    )
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        record = drill(args.source.resolve(), args.revision)
    except (
        RuntimeError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
    ) as error:
        record = {"passed": False, "error": str(error), "revision": args.revision}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: record.get(k) for k in ("passed", "error")}))
    for step in record.get("steps", []):
        print(
            ("PASS " if step["ok"] else "FAIL ")
            + f"§{step['section']}: "
            + step["command"].splitlines()[0][:100]
        )
    return 0 if record.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
