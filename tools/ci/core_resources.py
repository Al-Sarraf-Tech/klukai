"""Bounded Docker execution with pre-create ownership and awaited cleanup."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from typing import Any

from .core_source import write_json
from .cancellation import cancellation_scope, checkpoint

LABEL = "cc.appnest.klukai.core-run"
HEX = re.compile(r"[a-f0-9]{64}\Z")
# Disposable passwords are scrubbed before inspection output is retained.
CONTAINER_VIEW = '{"Id":{{json .Id}},"Name":{{json .Name}},"Image":{{json .Image}},"Config":{"Image":{{json .Config.Image}},"Labels":{{json .Config.Labels}},"User":{{json .Config.User}},"Env":{{json .Config.Env}}},"State":{{json .State}},"HostConfig":{{json .HostConfig}},"Mounts":{{json .Mounts}},"NetworkSettings":{"Networks":{{json .NetworkSettings.Networks}}}}'


class ResourceFailure(RuntimeError):
    """A resource or command could not be verified or safely cleaned."""


class Docker:
    def __init__(self, output: Path) -> None:
        self.output = output
        self.commands: list[dict[str, Any]] = []
        self.secrets: list[str] = []
        config = output / "docker-config"
        config.mkdir(mode=0o700, exist_ok=True)
        self.environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LC_ALL": "C.UTF-8",
            "DOCKER_CONFIG": str(config),
        }

    def scrub(self, text: str) -> str:
        for value in self.secrets:
            text = text.replace(value, "<disposable-credential>")
        return text

    def run(
        self,
        *args: str,
        environment: dict[str, str] | None = None,
        timeout: float = 30,
        check: bool = True,
        private_stdout: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        checkpoint()
        command = ["docker", *args]
        number = len(self.commands)
        record: dict[str, Any] = {
            "argv": command,
            "timeout_seconds": timeout,
            "exit_code": None,
        }
        self.commands.append(record)
        result: subprocess.CompletedProcess[str] | None = None
        stdout, stderr = "", ""
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=timeout,
                env={**self.environment, **(environment or {})},
            )
            record["exit_code"] = result.returncode
            stdout, stderr = result.stdout, result.stderr
        except BaseException as exc:
            record["error_type"] = type(exc).__name__
            stdout = getattr(exc, "stdout", "") or ""
            stderr = getattr(exc, "stderr", "") or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            raise
        finally:
            with cancellation_scope() as cancellation, cancellation.deferred():
                if private_stdout:
                    stdout = (
                        "<private inspection bytes sha256="
                        + hashlib.sha256(stdout.encode()).hexdigest()
                        + ">\n"
                    )
                for stream, content in [("stdout", stdout), ("stderr", stderr)]:
                    name = f"docker-{number:03d}.{stream}"
                    (self.output / name).write_text(self.scrub(content))
                    record[stream] = name
                write_json(self.output / "docker-commands.json", self.commands)
            if result is not None:
                cancellation.checkpoint()
        if result is None:
            raise ResourceFailure("Docker returned no result")
        if check and result.returncode:
            raise ResourceFailure(
                f"Docker {args[0]} failed; see private raw command output"
            )
        return result

    def container(self, identity: str) -> dict[str, Any] | None:
        result = self.run(
            "inspect",
            "--type=container",
            "--format=" + CONTAINER_VIEW,
            identity,
            check=False,
        )
        if result.returncode:
            if re.search(
                r"No such (?:object|container): " + re.escape(identity) + r"(?:\s|$)",
                result.stderr,
            ):
                return None
            raise ResourceFailure("Cannot inspect owned container")
        data = json.loads(result.stdout)
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if not isinstance(data, dict):
            raise ResourceFailure("Malformed container inspect")
        return data


class Resources:
    def __init__(
        self, docker: Docker, output: Path, *, network_required: bool = True
    ) -> None:
        self.docker, self.output = docker, output
        self.network_required = network_required
        self.token = secrets.token_hex(16)
        self.network = "klukai-core-ci-" + self.token
        self.state: dict[str, Any] = {
            "token": self.token,
            "network": {"name": self.network},
            "containers": [],
            "cleaned": False,
        }
        self.save()

    def save(self) -> None:
        write_json(self.output / "resources.json", self.state)

    def create_network(self) -> None:
        # Persist the unique name/label before Docker can create anything.
        result = self.docker.run(
            "network",
            "create",
            "--internal",
            "--label=" + LABEL + "=" + self.token,
            self.network,
        )
        identity = result.stdout.strip()
        if not HEX.fullmatch(identity):
            raise ResourceFailure("Invalid network identity")
        self.state["network"]["id"] = identity
        self.save()
        info = self.network_info()
        if info is None or info.get("Internal") is not True:
            raise ResourceFailure("Core network must be private/internal")

    def network_info(self) -> dict[str, Any] | None:
        result = self.docker.run("network", "inspect", self.network, check=False)
        if result.returncode:
            if re.search(
                r"(?:No such network: "
                + re.escape(self.network)
                + r"|network "
                + re.escape(self.network)
                + r" not found)(?:\s|$)",
                result.stderr,
            ):
                return None
            raise ResourceFailure("Cannot inspect owned network")
        data = json.loads(result.stdout)
        if (
            not isinstance(data, list)
            or len(data) != 1
            or not isinstance(data[0], dict)
        ):
            raise ResourceFailure("Malformed network inspect")
        info: dict[str, Any] = data[0]
        if (info.get("Labels") or {}).get(LABEL) != self.token or not HEX.fullmatch(
            info.get("Id", "")
        ):
            raise ResourceFailure("Network ownership mismatch")
        expected = self.state["network"].get("id")
        if expected is not None and expected != info["Id"]:
            raise ResourceFailure("Network identity mismatch")
        return info

    def create_container(
        self,
        role: str,
        image: str,
        flags: list[str],
        *,
        environment: dict[str, str] | None = None,
        command: list[str] | None = None,
    ) -> str:
        name = self.network + "-" + role
        resource: dict[str, Any] = {
            "role": role,
            "name": name,
            "image": image,
            "removed": False,
        }
        self.state["containers"].append(resource)
        self.save()
        args = [
            "create",
            "--name=" + name,
            "--label=" + LABEL + "=" + self.token,
            "--pull=never",
            *flags,
            image,
            *(command or []),
        ]
        result = self.docker.run(*args, environment=environment)
        identity = result.stdout.strip()
        if not HEX.fullmatch(identity):
            raise ResourceFailure("Invalid container identity")
        resource["id"] = identity
        self.save()
        return identity

    def owned(self, resource: dict[str, Any]) -> dict[str, Any] | None:
        info = self.docker.container(resource["name"])
        if info is None:
            return None
        cid = info.get("Id", "")
        config = info.get("Config") or {}
        if (
            not HEX.fullmatch(cid)
            or config.get("Image") != resource["image"]
            or (config.get("Labels") or {}).get(LABEL) != self.token
            or (resource.get("id") and cid != resource["id"])
        ):
            raise ResourceFailure("Container ownership mismatch")
        return info

    def attach(self, cid: str, *, timeout: float) -> dict[str, int]:
        result = self.docker.run("start", "--attach", cid, timeout=timeout, check=False)
        info = self.docker.container(cid)
        if info is None or info["State"].get("Running") is not False:
            raise ResourceFailure("Attached core container has not exited")
        exit_code = info["State"].get("ExitCode")
        if type(exit_code) is not int:
            raise ResourceFailure("Missing actual container exit code")
        return {"attach_exit_code": result.returncode, "container_exit_code": exit_code}

    def remove_container(self, resource: dict[str, Any]) -> None:
        info = self.owned(resource)
        if info is None:
            resource["removed"] = True
            return
        cid = info["Id"]
        if info["State"].get("Running"):
            try:
                stopped = self.docker.run(
                    "stop", "--time=3", cid, timeout=15, check=False
                )
                needs_kill = stopped.returncode != 0
            except (subprocess.SubprocessError, OSError):
                needs_kill = True
            if needs_kill:
                self.docker.run("kill", cid)
        waited = self.docker.run("wait", cid, timeout=20)
        resource["wait_exit_code"] = int(waited.stdout.strip())
        final = self.owned(resource)
        if final is None or final["State"].get("Running") is not False:
            raise ResourceFailure("Owned container did not stop")
        resource["actual_exit_code"] = final["State"]["ExitCode"]
        mismatch = (
            type(resource["actual_exit_code"]) is not int
            or resource["actual_exit_code"] != resource["wait_exit_code"]
        )
        self.docker.run("rm", "--volumes", cid)
        if self.docker.container(resource["name"]) is not None:
            raise ResourceFailure("Owned container was not removed")
        resource["removed"] = True
        if mismatch:
            raise ResourceFailure("Owned container wait/actual exit status differs")

    def close(self) -> None:
        with cancellation_scope() as cancellation, cancellation.deferred():
            self._close()

    def _close(self) -> None:
        errors = []
        for resource in reversed(self.state["containers"]):
            try:
                self.remove_container(resource)
            except (
                ResourceFailure,
                subprocess.SubprocessError,
                OSError,
                ValueError,
            ) as exc:
                errors.append(str(exc))
            self.save()
        try:
            info = self.network_info() if self.network_required else None
            if info is not None:
                if info.get("Containers"):
                    raise ResourceFailure("Owned network still has container endpoints")
                self.docker.run("network", "rm", info["Id"])
                if self.network_info() is not None:
                    raise ResourceFailure("Owned network was not removed")
            self.state["network"]["removed"] = True
        except (
            ResourceFailure,
            subprocess.SubprocessError,
            OSError,
            ValueError,
        ) as exc:
            errors.append(str(exc))
        self.state["cleaned"] = not errors
        self.state["cleanup_errors"] = errors
        self.save()
        if errors:
            raise ResourceFailure("; ".join(errors))
