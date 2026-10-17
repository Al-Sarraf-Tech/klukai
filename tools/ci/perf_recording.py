"""Private bounded Docker transcript with deliberately projected inspections."""

from __future__ import annotations

from contextvars import ContextVar
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from .core_source import write_json, require
from .cancellation import cancellation_scope

# Replaces every disposable secret value in retained command records.
SCRUBBED = "<disposable-credential>"

LABEL = "cc.appnest.klukai.perf-run"
BASE_ENV = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "PYTHON_VERSION",
        "PYTHON_SHA256",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "PG_MAJOR",
        "PG_VERSION",
        "PG_SHA256",
        "GOSU_VERSION",
        "PGDATA",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "LD_LIBRARY_PATH",
        "HOME",
        "XDG_CACHE_HOME",
        "DOCKER_PG_LLVM_DEPS",
        "VALKEY_VERSION",
        "TZ",
        "RUN_MODE",
        "DIR",
        "GPG_KEY",
    }
)
WORKER_ENV = frozenset(
    {
        "DATABASE_URL",
        "REDIS_URL",
        "QDRANT_URL",
        "PERSONALITY_PATH",
        "SEED_PASSWORD_JALSARRAF",
        "SEED_PASSWORD_RICKY",
        "LM_STUDIO_URL",
        "LM_STUDIO_TOKEN",
        "MCP_URL",
        "DATA_URL",
        "INFERENCE_URL",
        "VOICE_URL",
        "VOICE_STT_URL",
        "VOICE_BACKEND",
        "VOICE_ASYNC_JOBS",
        "VOICE_API_TOKEN",
        "COMFYUI_URL",
        "KLUKAI_CLAUDE_PRIMARY",
        "KLUKAI_BRAVE_ENABLED",
        "KLUKAI_CLAUDE_CREATIVE_ENABLED",
        "KLUKAI_IMAGE_PROVIDER",
        "KLUKAI_ACCOUNT_IMAGE_WORKER_URL",
        "KLUKAI_CLOUD_IMAGE_DAILY_LIMIT",
        "KLUKAI_CLOUD_IMAGE_MONTHLY_LIMIT",
        "HER_POV_EXECUTION",
        "AUDIO_DIR",
        "IMAGES_DIR",
        "OTEL_SDK_DISABLED",
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
        "POSTGRES_DB",
        "QDRANT__TELEMETRY_DISABLED",
    }
)

BACKEND_ENV_KEYS = {
    "postgres": {
        "PATH",
        "GOSU_VERSION",
        "LANG",
        "PG_MAJOR",
        "PG_VERSION",
        "PG_SHA256",
        "DOCKER_PG_LLVM_DEPS",
        "PGDATA",
    },
    "redis": {"PATH", "VALKEY_VERSION"},
    "qdrant": {"PATH", "TZ", "RUN_MODE", "DIR"},
    "fixture": {"PATH", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256"},
}


def projected_env(config: dict[str, Any], allowed: frozenset[str]) -> dict[str, str]:
    rows = config["Env"]
    require(type(rows) is list, "Missing projected environment")
    result = {}
    for row in rows:
        require(
            type(row) is dict
            and type(row.get("key")) is str
            and row["key"] not in result,
            "Duplicate/invalid environment",
        )
        key = row["key"]
        require(
            key in allowed
            and set(row) == {"key", "value"}
            and type(row["value"]) is str
            and row["value"].startswith(key + "="),
            "Unknown/inherited provider environment",
        )
        result[key] = row["value"][len(key) + 1 :]
    return result


def base_environment(config: dict[str, Any], role: str) -> dict[str, str]:
    result = projected_env(config, BASE_ENV)
    allowed = BACKEND_ENV_KEYS.get(
        role,
        {
            "PATH",
            "LANG",
            "LC_ALL",
            "PYTHON_VERSION",
            "PYTHON_SHA256",
            "GPG_KEY",
            "PYTHONUNBUFFERED",
            "PYTHONDONTWRITEBYTECODE",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "LD_LIBRARY_PATH",
            "HOME",
            "XDG_CACHE_HOME",
            "TZ",
        },
    )
    require(
        set(result) == allowed if role in BACKEND_ENV_KEYS else set(result) <= allowed,
        "Image base environment keys differ",
    )
    patterns = {
        "PATH": r"/(?:usr|bin|sbin|opt)[A-Za-z0-9_./-]*(?::/(?:usr|bin|sbin|opt)[A-Za-z0-9_./-]*)*",
        "LD_LIBRARY_PATH": r"/(?:usr|lib|opt)[A-Za-z0-9_./-]*(?::/(?:usr|lib|opt)[A-Za-z0-9_./-]*)*",
        "LANG": r"[A-Za-z0-9_.-]+",
        "LC_ALL": r"[A-Za-z0-9_.-]+",
        "GPG_KEY": r"[A-Fa-f0-9]{40}",
        "PG_SHA256": r"[a-f0-9]{64}",
        "PYTHON_SHA256": r"[a-f0-9]{64}",
        "GOSU_VERSION": r"[0-9.]+",
        "PYTHON_VERSION": r"[0-9.]+",
        "PG_MAJOR": r"17",
        "PG_VERSION": r"17[0-9A-Za-z.+-]*",
        "VALKEY_VERSION": r"[0-9.]+",
        # The reviewed postgres:17-alpine image separates these with tabs.
        "DOCKER_PG_LLVM_DEPS": r"[A-Za-z0-9 \t.+_-]+",
        "PGDATA": r"/var/lib/postgresql/data(?:/pgdata)?",
        "HOME": r"/home/(?:appuser|nonroot)",
        "XDG_CACHE_HOME": r"/tmp/klukai-cache",
        "SSL_CERT_FILE": r"/etc/ssl/certs/ca-certificates.crt",
        "SSL_CERT_DIR": r"/etc/ssl/certs",
        "PYTHONUNBUFFERED": r"1",
        "PYTHONDONTWRITEBYTECODE": r"1",
        "TZ": r"(?:Etc/UTC|UTC)",
        "RUN_MODE": r"production",
        "DIR": r"",
    }
    require(
        all(re.fullmatch(patterns[key], value) for key, value in result.items()),
        "Unsafe image base environment value",
    )
    require(
        not any(".." in value.split("/") for value in result.values()),
        "Escaping image environment path",
    )
    return result


def field(parent: str, key: str) -> str:
    """Newer Docker image stores omit empty keys; index yields null instead of failing."""
    return "{{json (index " + parent + " " + json.dumps(key) + ")}}"


def label(parent: str, key: str) -> str:
    return (
        "{{with index "
        + parent
        + ' "Labels"}}{{json (index . '
        + json.dumps(key)
        + ")}}{{else}}null{{end}}"
    )


def env_view(allowed: frozenset[str]) -> str:
    condition = "or " + " ".join(
        "(eq $key " + json.dumps(k) + ")" for k in sorted(allowed)
    )
    return (
        '[{{range $i,$item := index .Config "Env"}}{{if $i}},{{end}}'
        '{{$parts := split $item "="}}{{$key := index $parts 0}}'
        '{"key":{{json $key}},{{if ' + condition + '}}"value":{{json $item}}'
        '{{else}}"nonempty":{{ne $item (print $key "=")}}{{end}}}{{end}}]'
    )


IMAGE_VIEW = (
    '{"Id":{{json .Id}},"Os":{{json .Os}},"Architecture":{{json .Architecture}},'
    '"Config":{"User":'
    + field(".Config", "User")
    + ',"Entrypoint":'
    + field(".Config", "Entrypoint")
    + ',"Cmd":'
    + field(".Config", "Cmd")
    + ',"Revision":'
    + label(".Config", "org.opencontainers.image.revision")
    + ","
    '"Env":' + env_view(BASE_ENV) + "}}"
)
HOST_FIELDS = tuple(
    (
        "NanoCpus Memory MemorySwap PidsLimit ReadonlyRootfs Privileged NetworkMode CapDrop SecurityOpt "
        "Binds PortBindings Devices DeviceRequests VolumesFrom Links ExtraHosts Dns DnsSearch GroupAdd "
        "PublishAllPorts PidMode UTSMode CgroupParent IpcMode CapAdd Tmpfs CpuShares CpuPeriod CpuQuota "
        "CpusetCpus CpusetMems AutoRemove Runtime DeviceCgroupRules Sysctls StorageOpt CgroupnsMode RestartPolicy OomKillDisable"
    ).split()
)
HOST_VIEW = (
    "{"
    + ",".join('"' + key + '":' + field(".HostConfig", key) for key in HOST_FIELDS)
    + "}"
)
CONTAINER_VIEW = (
    '{"Id":{{json .Id}},"Name":{{json .Name}},"Image":{{json .Image}},'
    '"Config":{"Image":'
    + field(".Config", "Image")
    + ',"User":'
    + field(".Config", "User")
    + ',"Entrypoint":'
    + field(".Config", "Entrypoint")
    + ',"Cmd":'
    + field(".Config", "Cmd")
    + ',"Owner":'
    + label(".Config", LABEL)
    + ","
    '"Env":' + env_view(BASE_ENV | WORKER_ENV) + "},"
    '"State":{"Running":{{json .State.Running}},"ExitCode":{{json .State.ExitCode}},"OOMKilled":{{json .State.OOMKilled}}},'
    '"HostConfig":' + HOST_VIEW + ',"Mounts":{{json .Mounts}},'
    '"NetworkSettings":{"Networks":{{json .NetworkSettings.Networks}}}}'
)
NETWORK_VIEW = (
    '{"Id":{{json .Id}},"Name":{{json .Name}},"Internal":{{json .Internal}},'
    '"Owner":' + label(".", LABEL) + ',"Containers":' + field(".", "Containers") + "}"
)
VOLUME_VIEW = (
    '{"Name":{{json .Name}},"Driver":{{json .Driver}},'
    '"Owner":' + label(".", LABEL) + ',"Options":' + field(".", "Options") + "}"
)
ACTIVE: ContextVar["Recorder | None"] = ContextVar("perf_recorder", default=None)


class Recorder:
    """Never load the owner's Docker config or inherit the owner's environment."""

    def __init__(self, output: Path) -> None:
        self.output = output
        self.rows: list[dict[str, Any]] = []
        self.secrets: list[str] = []
        config = output / "docker-config"
        config.mkdir(mode=0o700)
        self.environment = {
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C.UTF-8",
            "DOCKER_CONFIG": str(config),
        }

    def scrub(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, SCRUBBED)
        return value

    def run(
        self,
        *args: str,
        environment: dict[str, str] | None = None,
        stdin: str | None = None,
        check: bool = True,
    ) -> str:
        row: dict[str, Any] = {
            "argv": ["docker", *args],
            "timeout_seconds": 50,
            "exit_code": None,
            "environment": self.scrub(json.dumps(environment or {}, sort_keys=True)),
            "stdin": stdin,
        }
        number = len(self.rows)
        self.rows.append(row)
        stdout, stderr = "", ""
        try:
            result = subprocess.run(
                row["argv"],
                text=True,
                capture_output=True,
                timeout=50,
                input=stdin,
                env={**self.environment, **(environment or {})},
            )
            stdout, stderr = result.stdout, result.stderr
            row["exit_code"] = result.returncode
        except BaseException as error:
            row["error_type"] = type(error).__name__
            stdout, stderr = (
                getattr(error, "stdout", "") or "",
                getattr(error, "stderr", "") or "",
            )
            raise
        finally:
            with cancellation_scope() as latch, latch.deferred():
                for name, value in (("stdout", stdout), ("stderr", stderr)):
                    filename = f"docker-{number:03d}.{name}"
                    if isinstance(value, bytes):
                        value = value.decode(errors="replace")
                    (self.output / filename).write_text(self.scrub(value))
                    row[name] = filename
                write_json(self.output / "docker-commands.json", self.rows)
        if check and result.returncode:
            raise RuntimeError(
                "Disposable Docker command failed; inspect private transcript"
            )
        return stdout + stderr if args[0] == "logs" else stdout.strip()


def flags(
    dependency: str,
    name: str,
    network: str,
    volume: str,
    *,
    env: dict[str, str],
    image: str,
) -> list[str]:
    from .perf_resources import RESOURCES

    resource = RESOURCES[dependency]
    result = [
        "create",
        "--pull=never",
        "--name",
        name,
        "--label",
        LABEL + "=" + network,
        "--network",
        network,
        "--network-alias",
        dependency,
        "--cpus",
        str(resource["nano_cpus"] / 1e9),
        "--memory",
        str(resource["memory_bytes"]),
        "--memory-swap",
        str(resource["memory_bytes"]),
        "--pids-limit",
        "128",
        "--read-only",
        "--security-opt",
        "no-new-privileges",
        "--cap-drop",
        "ALL",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m,mode=1777",
    ]
    additions = {
        "postgres": [
            x
            for cap in ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID")
            for x in ("--cap-add", cap)
        ]
        + [
            "--tmpfs",
            "/var/lib/postgresql/data:rw,size=256m",
            "--tmpfs",
            "/var/run/postgresql:rw,size=8m",
        ],
        "qdrant": [
            "--tmpfs",
            "/qdrant/storage:rw,size=128m",
            "--tmpfs",
            "/qdrant/snapshots:rw,size=16m",
        ],
        "redis": ["--user", "1000:1000"],
        "core": [
            "--user",
            "1000:1000",
            "--tmpfs",
            "/images:rw,size=32m,uid=1000,gid=1000",
            "--tmpfs",
            "/audio:rw,size=16m,uid=1000,gid=1000",
        ],
        "fixture": [],
    }
    result += additions[dependency]
    if dependency in {"core", "fixture"}:
        result += [
            "--mount",
            f"type=volume,source={volume},target=/perf-fixtures,readonly",
        ]
    if dependency == "fixture":
        result += ["--entrypoint", "/usr/local/bin/python"]
    for key in env:
        result += ["--env", key]
    result += [image]
    if dependency == "redis":
        result += ["valkey-server", "--save", "", "--appendonly", "no"]
    elif dependency == "fixture":
        result += ["/perf-fixtures/perf_fixtures.py"]
    return result
