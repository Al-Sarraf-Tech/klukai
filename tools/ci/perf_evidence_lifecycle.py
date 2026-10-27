"""Consume the complete ordered disposable producer transcript and profiles."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from .perf_evidence_identity import document, loads, require
from .perf_recording import (
    BASE_ENV,
    base_environment,
    projected_env,
    flags,
    CONTAINER_VIEW,
    IMAGE_VIEW,
    LABEL,
    NETWORK_VIEW,
    VOLUME_VIEW,
    HOST_FIELDS,
    SCRUBBED,
)
from .perf_mcp import MCP_CONFIG, MCP_CONFIG_NAME
from .perf_resources import RESOURCES
from .perf_stack import environment


class _ScrubbedStack:
    """The canonical environment factory is source-bound; no copied flags are trusted."""

    password = SCRUBBED


def host_profile(
    host: dict[str, Any],
    dependency: str,
    name: str,
    network: str,
    volume: str,
    env: dict[str, str],
    image: dict[str, Any],
) -> None:
    require(
        set(host) == set(HOST_FIELDS), "Unknown/missing projected Docker profile field"
    )
    required = {
        "NanoCpus": RESOURCES[dependency]["nano_cpus"],
        "Memory": RESOURCES[dependency]["memory_bytes"],
        "MemorySwap": RESOURCES[dependency]["memory_bytes"],
        "PidsLimit": 128,
        "ReadonlyRootfs": True,
        "Privileged": False,
        "NetworkMode": network,
        "CapDrop": ["ALL"],
    }
    require(
        all(type(host.get(k)) is type(v) and host[k] == v for k, v in required.items()),
        "Actual resource/security profile differs",
    )
    require(
        host["SecurityOpt"] in (["no-new-privileges"], ["no-new-privileges:true"]),
        "Actual security options differ",
    )
    for key in (
        "Binds",
        "PortBindings",
        "Devices",
        "DeviceRequests",
        "VolumesFrom",
        "Links",
        "ExtraHosts",
        "Dns",
        "DnsSearch",
        "GroupAdd",
        "PublishAllPorts",
        "PidMode",
        "UTSMode",
        "CgroupParent",
        "CpuShares",
        "CpuPeriod",
        "CpuQuota",
        "CpusetCpus",
        "CpusetMems",
        "AutoRemove",
        "DeviceCgroupRules",
        "Sysctls",
        "StorageOpt",
        "OomKillDisable",
    ):
        require(not host.get(key), "Unsafe actual Docker profile: " + key)
    require(host.get("IpcMode", "private") in {"", "private"}, "Unsafe IPC mode")
    require(
        host["Runtime"] in {"", "runc"} and host["CgroupnsMode"] in {"", "private"},
        "Unsafe runtime/cgroup mode",
    )
    require(
        host["RestartPolicy"]
        in (
            {"Name": "", "MaximumRetryCount": 0},
            {"Name": "no", "MaximumRetryCount": 0},
        ),
        "Unsafe restart policy",
    )
    expected_caps = (
        ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"]
        if dependency == "postgres"
        else []
    )
    # Newer Docker engines report added capabilities as CAP_CHOWN, not CHOWN.
    actual_caps = sorted(cap.removeprefix("CAP_") for cap in host.get("CapAdd") or [])
    require(actual_caps == expected_caps, "Actual capabilities differ")
    argv = flags(dependency, name, network, volume, env=env, image=image["Id"])
    tmpfs = dict(
        item.split(":", 1)
        for index, item in enumerate(argv)
        if index and argv[index - 1] == "--tmpfs"
    )
    require(host["Tmpfs"] == tmpfs, "Actual tmpfs profile differs")


def profile(
    info: dict[str, Any],
    dependency: str,
    name: str,
    network: str,
    volume: str,
    image: dict[str, Any],
    env: dict[str, str],
    *,
    running: bool,
) -> None:
    require(
        info["Name"] == "/" + name
        and re.fullmatch(r"[a-f0-9]{64}", info["Id"])
        and info["Image"] == image["Id"]
        and info["Config"]["Image"] == image["Id"]
        and info["Config"]["Owner"] == network,
        "Actual container identity/ownership differs",
    )
    host_profile(info["HostConfig"], dependency, name, network, volume, env, image)
    expected_mounts = (
        [
            {
                "Type": "volume",
                "Name": volume,
                "Destination": "/perf-fixtures",
                "RW": False,
            }
        ]
        if dependency in {"core", "fixture"}
        else []
    )
    mounts = [
        {key: m[key] for key in ("Type", "Name", "Destination", "RW")}
        for m in info["Mounts"]
    ]
    require(mounts == expected_mounts, "Unexpected actual mounts")
    config = info["Config"]
    # The containerd image store omits an empty User (null) where container
    # inspect reports "": both mean the image default.
    expected_user = (
        "1000:1000"
        if dependency in {"core", "redis"}
        else image["Config"]["User"] or ""
    )
    require((config["User"] or "") == expected_user, "Effective container USER differs")
    entrypoint = (
        ["/usr/local/bin/python"]
        if dependency == "fixture"
        else image["Config"]["Entrypoint"]
    )
    cmd = (
        ["/perf-fixtures/perf_fixtures.py"]
        if dependency == "fixture"
        else ["valkey-server", "--save", "", "--appendonly", "no"]
        if dependency == "redis"
        else image["Config"]["Cmd"]
    )
    require(
        config["Entrypoint"] == entrypoint and config["Cmd"] == cmd,
        "Effective entrypoint/command differs",
    )
    actual_env = projected_env(config, BASE_ENV | frozenset(env))
    base = base_environment(image["Config"], dependency)
    require(
        actual_env == {**base, **env}, "Actual exact worker/base environment differs"
    )
    networks = info["NetworkSettings"]["Networks"]
    # Newer Docker engines clear the address once a container stops.
    require(
        set(networks) == {network}
        and type(networks[network]["IPAddress"]) is str
        and (bool(networks[network]["IPAddress"]) or not running),
        "Actual isolated network differs",
    )
    state = info["State"]
    require(
        state.get("Running") is running
        and state.get("OOMKilled") is False
        and type(state.get("ExitCode")) is int
        and state["ExitCode"] == 0,
        "Actual execution/exit state differs",
    )


class Sequence:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows, self.index = rows, 0

    def take(
        self,
        args: list[str],
        *,
        success: bool = True,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> dict[str, Any]:
        require(self.index < len(self.rows), "Incomplete producer sequence")
        row = self.rows[self.index]
        require(
            row["argv"] == ["docker", *args],
            f"Unexpected/reordered command at {self.index}",
        )
        self.index += 1
        require((row["exit_code"] == 0) is success, "Unexpected Docker exit")
        require(
            loads(row["environment"]) == (env or {}) and row["stdin"] == stdin,
            "Unexpected inherited environment/stdin",
        )
        return row

    def inspect(self, kind: str, name: str, *, absent: bool = False) -> dict[str, Any]:
        view = {
            "container": CONTAINER_VIEW,
            "network": NETWORK_VIEW,
            "volume": VOLUME_VIEW,
        }[kind]
        argv = ["inspect"] if kind == "container" else [kind, "inspect"]
        row = self.take([*argv, "--format=" + view, name], success=not absent)
        if absent:
            require(
                not row["stdout_text"].strip()
                and re.search(
                    r"(?:(?i:no such) (?:object|container|network|volume): "
                    + re.escape(name)
                    + r"|(?:network|volume) "
                    + re.escape(name)
                    + r" not found|get "
                    + re.escape(name)
                    + r": no such volume)(?:\s|$)",
                    row["stderr_text"],
                ),
                "Actual absence unproven",
            )
            return {}
        require(not row["stderr_text"], "Unexpected inspect stderr")
        info = loads(row["stdout_text"])
        require(type(info) is dict, "Malformed projected inspection")
        return info


def images_bound(
    sequence: Sequence, reports: dict[str, Any], inputs: dict[str, Any]
) -> dict[str, Any]:
    images = {}
    for role in ("baseline", "candidate"):
        declared = reports[role]["image"]
        row = sequence.take(
            ["image", "inspect", "--format=" + IMAGE_VIEW, declared["input"]]
        )
        info = loads(row["stdout_text"])
        require(
            type(info) is dict
            and info["Id"] == declared["id"]
            and info["Config"]["Revision"] == declared["declared_revision"]
            and info["Os"] == "linux"
            and info["Architecture"] == "amd64",
            "Actual image/source/platform mismatch",
        )
        base_environment(info["Config"], "core")
        images[role] = info
    context = sequence.take(
        ["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
    )
    require(
        context["stdout_text"].strip().startswith("unix://"), "Nonlocal Docker endpoint"
    )
    for role in ("postgres", "redis", "qdrant", "fixture"):
        image = inputs["backend_images"][role]
        row = sequence.take(
            [
                "image",
                "inspect",
                "--format=" + IMAGE_VIEW,
                inputs["backend_inputs"][role],
            ]
        )
        info = loads(row["stdout_text"])
        require(
            info["Id"] == image
            and info["Os"] == "linux"
            and info["Architecture"] == "amd64",
            "Backend actual image differs",
        )
        base_environment(info["Config"], role)
        images[role] = info
    return images


def lifecycle(
    output: Path,
    reports: dict[str, Any],
    inputs: dict[str, Any],
    rows: list[dict[str, Any]],
) -> str:
    sequence = Sequence(rows)
    images = images_bound(sequence, reports, inputs)
    cleanup = document(output / "cleanup.json")
    prefix = cleanup["prefix"]
    require(
        type(prefix) is str and re.fullmatch(r"klukai-s-perf-[a-f0-9]{10}", prefix),
        "Invalid disposable ownership",
    )
    volume = prefix + "-fixtures"
    created = sequence.take(
        ["network", "create", "--internal", "--label", LABEL + "=" + prefix, prefix]
    )
    network = sequence.inspect("network", prefix)
    require(
        network["Id"] == created["stdout_text"].strip()
        and re.fullmatch(r"[a-f0-9]{64}", network["Id"])
        and network["Name"] == prefix
        and network["Internal"] is True
        and network["Owner"] == prefix
        and not network["Containers"],
        "Owned internal network differs",
    )
    sequence.take(["volume", "create", "--label", LABEL + "=" + prefix, volume])
    actual_volume = sequence.inspect("volume", volume)
    require(
        actual_volume
        == {"Name": volume, "Driver": "local", "Owner": prefix, "Options": None},
        "Owned fixture volume differs",
    )
    writer = prefix + "-volume-writer"
    program = "import json,sys,pathlib,os; p=pathlib.Path('/fixture'); os.chmod(p,0o755); [(p/k).write_text(v) for k,v in json.load(sys.stdin).items()]"
    payload = {
        "perf_fixtures.py": (output / "source/tools/ci/perf_fixtures.py").read_text(),
        "personality.yaml": (output / "source/config/personality.yaml").read_text(),
        MCP_CONFIG_NAME: MCP_CONFIG,
    }
    sequence.take(
        [
            "run",
            "--rm",
            "--name",
            writer,
            "--label",
            LABEL + "=" + prefix,
            "-i",
            "--pull=never",
            "--network",
            "none",
            "--cpus",
            ".1",
            "--memory",
            "67108864",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            f"type=volume,source={volume},target=/fixture",
            "--entrypoint",
            "/usr/local/bin/python",
            inputs["backend_images"]["fixture"],
            "-c",
            program,
        ],
        stdin=__import__("json").dumps(payload),
    )
    sequence.inspect("container", writer, absent=True)
    backend_envs = {
        "postgres": {
            "POSTGRES_PASSWORD": SCRUBBED,
            "POSTGRES_USER": "perf_test",
            "POSTGRES_DB": "postgres",
        },
        "redis": {},
        "qdrant": {"QDRANT__TELEMETRY_DISABLED": "true"},
        "fixture": {},
    }

    def container(
        role: str, name: str, image: dict[str, Any], env: dict[str, str]
    ) -> dict[str, Any]:
        created = sequence.take(
            flags(role, name, prefix, volume, env=env, image=image["Id"]), env=env
        )
        require(
            re.fullmatch(r"[a-f0-9]{64}", created["stdout_text"].strip()),
            "Missing created container ID",
        )
        sequence.take(["start", name])
        info = sequence.inspect("container", name)
        require(
            info["Id"] == created["stdout_text"].strip(),
            "Actual created container changed",
        )
        profile(info, role, name, prefix, volume, image, env, running=True)
        return info

    for role, env in backend_envs.items():
        info = container(role, prefix + "-" + role, images[role], env)
        require(
            inputs["backends"][role]
            == {
                "container": prefix + "-" + role,
                "image": info["Image"],
                "ip": info["NetworkSettings"]["Networks"][prefix]["IPAddress"],
                "resources": RESOURCES[role],
            },
            "Backend report differs",
        )
    for _ in range(2):
        version = sequence.take(["version", "--format", "{{json .}}"])
        require(
            loads(version["stdout_text"]) == inputs["docker"],
            "Docker version conditions differ",
        )
    for index in range(reports["candidate"]["configuration"]["rounds"]):
        for role in (
            ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
        ):
            name = f"{prefix}-{role}-{index}"

            env = environment(_ScrubbedStack(), f"perf_{role}_{index}")
            info = container("core", name, images[role], env)
            actual = reports[role]["rounds"][index]["actual_container"]
            require(
                actual
                == {
                    "container": name,
                    "image": images[role]["Id"],
                    "ip": info["NetworkSettings"]["Networks"][prefix]["IPAddress"],
                    "resources": RESOURCES["core"],
                },
                "Measured container/profile differs",
            )
            sequence.take(["stop", "--time", "25", name])
            final = sequence.inspect("container", name)
            profile(
                final, "core", name, prefix, volume, images[role], env, running=False
            )
            require(final["Id"] == info["Id"], "Stopped container identity differs")
            log = sequence.take(["logs", name])
            require(
                (output / (name + ".log")).read_text()
                == log["stdout_text"] + log["stderr_text"],
                "Core log bytes differ",
            )
            sequence.take(["rm", "-v", name])
            sequence.inspect("container", name, absent=True)
    for role in reversed(backend_envs):
        name = prefix + "-" + role
        log = sequence.take(["logs", name])
        require(
            (output / (name + ".log")).read_text()
            == log["stdout_text"] + log["stderr_text"],
            "Backend log bytes differ",
        )
        sequence.take(["rm", "-fv", name])
        sequence.inspect("container", name, absent=True)
    for kind, name in (("volume", volume), ("network", prefix)):
        sequence.take([kind, "rm", name])
        sequence.inspect(kind, name, absent=True)
    remaining = sequence.take(["ps", "-aq", "--filter", "name=" + prefix])
    require(
        not remaining["stdout_text"].strip() and sequence.index == len(rows),
        "Extra/orphan commands or leaked resources",
    )
    require(
        cleanup["verified"] is True
        and cleanup
        == {
            "verified": True,
            "errors": [],
            "remaining_containers": "",
            "prefix": prefix,
        },
        "Cleanup report differs",
    )
    return prefix
