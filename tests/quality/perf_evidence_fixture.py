"""Synthetic timings/transcript, genuine Git archive, real producer resource calls.

Only the external Docker subprocess is replaced. No network/backend/model runs;
these bundles are unit fixtures and never operational performance evidence.
"""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import platform
import shutil
import subprocess
from unittest.mock import patch

import httpx

from tools.ci import core_source as source
from tools.ci.perf_evidence_identity import fingerprints, helpers
from tools.ci.perf_gate import compare
from tools.ci.perf_recording import ACTIVE, Recorder
from tools.ci.perf_resources import (
    BACKEND_IMAGES,
    RESOURCES,
    DisposableStack,
    docker,
    inspect_image,
)
from tools.ci.perf_schema import TARGETS, digest
from tools.ci.perf_stack import environment, write_exclusive

ACTUAL = Path(__file__).resolve().parents[2]


def rehash(output):
    manifest = json.loads((output / "manifest.json").read_text())
    manifest["raw_sha256"] = {
        name: value
        for name, value in source.inventory(output).items()
        if not name.startswith("source/")
        and name not in {"source.tar", "manifest.json"}
    }
    source.write_json(output / "manifest.json", manifest)


def bundle(tmp_path, millis=11):
    root, output = tmp_path / "repo", tmp_path / "bundle"
    (root / "tools/ci").mkdir(parents=True)
    for path in (ACTUAL / "tools/ci").glob("perf_*.py"):
        shutil.copyfile(path, root / "tools/ci" / path.name)
    for name in ("core_source.py", "cancellation.py"):
        shutil.copyfile(ACTUAL / "tools/ci" / name, root / "tools/ci" / name)
    for name in ("config/personality.yaml", "docker/core/app/observability/slos.json"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ACTUAL / name, root / name)
    for args in (
        ["init", "-q"],
        ["config", "user.name", "Unit fixture"],
        ["config", "user.email", "fixture@example.invalid"],
        ["config", "commit.gpgsign", "false"],
        ["config", "core.hooksPath", "/dev/null"],
        ["add", "."],
        ["commit", "-qm", "baseline"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    baseline = source.git(root, "rev-parse", "HEAD")
    (root / "candidate.txt").write_text("candidate\n")
    for args in (["add", "."], ["commit", "-qm", "candidate"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    revision = source.git(root, "rev-parse", "HEAD")
    output.mkdir(mode=0o700)
    captured = source.snapshot(root, output, revision, development=False)
    identities = {"sha256:" + "a" * 64: baseline, "sha256:" + "b" * 64: revision}
    images, containers, network, volume = {}, {}, {}, {}
    original = subprocess.run

    def boundary(argv, **kwargs):
        if argv[0] != "docker":
            return original(argv, **kwargs)
        args = argv[1:]
        text, error, code = "", "", 0
        if args[:2] == ["image", "inspect"]:
            image = args[-1]
            info = {
                "Id": image,
                "Os": "linux",
                "Architecture": "amd64",
                "Config": {
                    "Revision": identities.get(image),
                    "User": "1000:1000",
                    "Env": [],
                    "Entrypoint": ["/usr/local/bin/python"],
                    "Cmd": ["-m", "app"],
                },
            }
            base = {
                BACKEND_IMAGES["postgres"]: {
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "GOSU_VERSION": "1.19",
                    "LANG": "en_US.utf8",
                    "PG_MAJOR": "17",
                    "PG_VERSION": "17.6",
                    "PG_SHA256": "a" * 64,
                    "DOCKER_PG_LLVM_DEPS": "llvm19-dev clang19",
                    "PGDATA": "/var/lib/postgresql/data",
                },
                BACKEND_IMAGES["redis"]: {
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "VALKEY_VERSION": "8.0.1",
                },
                BACKEND_IMAGES["qdrant"]: {
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "TZ": "Etc/UTC",
                    "RUN_MODE": "production",
                    "DIR": "",
                },
                BACKEND_IMAGES["fixture"]: {
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "GPG_KEY": "A" * 40,
                    "PYTHON_VERSION": "3.14.0",
                    "PYTHON_SHA256": "a" * 64,
                },
            }.get(
                image,
                {
                    "PATH": "/opt/venv/bin:/usr/local/bin",
                    "PYTHONUNBUFFERED": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
            info["Config"]["Env"] = [
                {"key": k, "value": k + "=" + v} for k, v in base.items()
            ]
            images[image] = info
            text = json.dumps(info)
        elif args[:2] == ["context", "inspect"]:
            text = "unix:///var/run/docker.sock"
        elif args[0] == "version":
            text = '{"Server":{"Version":"unit-fixture"}}'
        elif args[:2] == ["network", "create"]:
            network.update(
                Id="c" * 64, Name=args[-1], Internal=True, Owner=args[-1], Containers={}
            )
            text = network["Id"]
        elif args[:2] == ["volume", "create"]:
            volume.update(
                Name=args[-1], Driver="local", Owner=network["Name"], Options=None
            )
            text = volume["Name"]
        elif args[:2] in (["network", "inspect"], ["volume", "inspect"]):
            data = network if args[0] == "network" else volume
            if data:
                text = json.dumps(data)
            else:
                code, error = (
                    1,
                    (
                        "Error response from daemon: get "
                        + args[-1]
                        + ": no such volume\n"
                        if args[0] == "volume"
                        else "Error: No such network: " + args[-1] + "\n"
                    ),
                )
        elif args[0] == "create":

            def value(key):
                return args[args.index(key) + 1]

            name = value("--name")
            image = next(item for item in args if item.startswith("sha256:"))
            base = images[image]["Config"]
            env = kwargs["env"]
            keys = [args[i + 1] for i, item in enumerate(args) if item == "--env"]
            mount = (
                [
                    {
                        "Type": "volume",
                        "Name": volume["Name"],
                        "Destination": "/perf-fixtures",
                        "RW": False,
                    }
                ]
                if "--mount" in args
                else []
            )
            host = {
                "NanoCpus": int(float(value("--cpus")) * 1e9),
                "Memory": int(value("--memory")),
                "MemorySwap": int(value("--memory-swap")),
                "PidsLimit": 128,
                "ReadonlyRootfs": True,
                "Privileged": False,
                "NetworkMode": network["Name"],
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "CapAdd": [
                    args[i + 1] for i, item in enumerate(args) if item == "--cap-add"
                ],
                "Tmpfs": dict(
                    args[i + 1].split(":", 1)
                    for i, item in enumerate(args)
                    if item == "--tmpfs"
                ),
            }
            host.update(
                {
                    "Binds": None,
                    "PortBindings": {},
                    "Devices": [],
                    "DeviceRequests": None,
                    "VolumesFrom": None,
                    "Links": None,
                    "ExtraHosts": None,
                    "Dns": [],
                    "DnsSearch": [],
                    "GroupAdd": None,
                    "PublishAllPorts": False,
                    "PidMode": "",
                    "UTSMode": "",
                    "CgroupParent": "",
                    "IpcMode": "private",
                    "CpuShares": 0,
                    "CpuPeriod": 0,
                    "CpuQuota": 0,
                    "CpusetCpus": "",
                    "CpusetMems": "",
                    "AutoRemove": False,
                    "Runtime": "runc",
                    "DeviceCgroupRules": None,
                    "Sysctls": None,
                    "StorageOpt": None,
                    "CgroupnsMode": "private",
                    "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                    "OomKillDisable": False,
                }
            )
            index = args.index(image)
            containers[name] = {
                "Id": digest(name),
                "Name": "/" + name,
                "Image": image,
                "Config": {
                    "Image": image,
                    "Owner": network["Name"],
                    "User": value("--user") if "--user" in args else base["User"],
                    "Entrypoint": [value("--entrypoint")]
                    if "--entrypoint" in args
                    else base["Entrypoint"],
                    "Cmd": args[index + 1 :] or base["Cmd"],
                    "Env": [
                        {"key": k, "value": k + "=" + v}
                        for k, v in {
                            **{
                                row["key"]: row["value"].split("=", 1)[1]
                                for row in base["Env"]
                            },
                            **{k: env[k] for k in keys},
                        }.items()
                    ],
                },
                "HostConfig": host,
                "Mounts": mount,
                "State": {"Running": False, "ExitCode": 0, "OOMKilled": False},
                "NetworkSettings": {
                    "Networks": {network["Name"]: {"IPAddress": "172.22.0.2"}}
                },
            }
            text = containers[name]["Id"]
        elif args[0] == "start":
            containers[args[-1]]["State"]["Running"] = True
        elif args[0] == "inspect":
            if args[-1] in containers:
                text = json.dumps(containers[args[-1]])
            else:
                code, error = 1, "Error: No such object: " + args[-1] + "\n"
        elif args[0] == "stop":
            containers[args[-1]]["State"]["Running"] = False
        elif args[0] == "rm":
            del containers[args[-1]]
        elif args[:2] == ["network", "rm"]:
            network.clear()
        elif args[:2] == ["volume", "rm"]:
            volume.clear()
        elif args[0] not in {"run", "logs", "ps"}:
            raise AssertionError("Unexpected external Docker boundary: " + str(args))
        return subprocess.CompletedProcess(argv, code, text, error)

    recorder = Recorder(output)
    token = ACTIVE.set(recorder)
    reports = {}
    policy = json.loads((root / "docker/core/app/observability/slos.json").read_text())
    started = datetime.now(UTC) - timedelta(seconds=20)
    config = {
        "samples": 100,
        "rounds": 3,
        "warmup": 10,
        "concurrency": 1,
        "seed_version": 1,
    }
    try:
        with patch("subprocess.run", boundary):
            for role, char, rev in (
                ("baseline", "a", baseline),
                ("candidate", "b", revision),
            ):
                image = "sha256:" + char * 64
                info = inspect_image(image)
                reports[role] = {
                    "schema": 1,
                    "role": role,
                    "run_id": "c" * 32,
                    "image": {
                        **info,
                        "input": image,
                        "declared_revision": rev,
                        "image_revision": rev,
                        "verified": True,
                    },
                    "started_at": (started + timedelta(seconds=1)).isoformat(),
                    "finished_at": datetime.now(UTC).isoformat(),
                    "configuration": config,
                    "resources": RESOURCES,
                    "development_only": False,
                    "rounds": [],
                    "cleanup_verified": True,
                    "cpu_temperature_max_c": 40.0,
                }
            stack = DisposableStack(output, root)
            docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
            for role, image in BACKEND_IMAGES.items():
                inspect_image(image, role=role)
            docker(
                "network",
                "create",
                "--internal",
                "--label",
                "cc.appnest.klukai.perf-run=" + stack.prefix,
                stack.network,
            )
            stack.network_created = True
            from tools.ci.perf_recording import NETWORK_VIEW

            docker("network", "inspect", "--format=" + NETWORK_VIEW, stack.network)
            stack._fixture_volume()
            for role in ("postgres", "redis", "qdrant", "fixture"):
                env = (
                    {
                        "POSTGRES_PASSWORD": stack.password,
                        "POSTGRES_USER": "perf_test",
                        "POSTGRES_DB": "postgres",
                    }
                    if role == "postgres"
                    else {"QDRANT__TELEMETRY_DISABLED": "true"}
                    if role == "qdrant"
                    else {}
                )
                name = stack._create(
                    role,
                    BACKEND_IMAGES[role],
                    env=env,
                    arguments=["valkey-server", "--save", "", "--appendonly", "no"]
                    if role == "redis"
                    else ["/perf-fixtures/perf_fixtures.py"]
                    if role == "fixture"
                    else None,
                    entrypoint="/usr/local/bin/python" if role == "fixture" else None,
                    fixture_volume=role == "fixture",
                )
                stack.backends[role] = stack.inspect_container(name, role)
            sources = {
                p.name: p.read_text()
                for p in sorted((root / "tools/ci").glob("perf_*.py"))
            }
            write_exclusive(
                output / "harness-sources.json",
                {"files": sources, "sha256": digest(sources)},
            )
            version = json.loads(docker("version", "--format", "{{json .}}"))
            host = {
                "platform": "Linux-unit",
                "machine": "x86_64",
                "cpu": "processor: 0\n",
                "docker": docker("version", "--format", "{{json .}}"),
            }
            write_exclusive(
                output / "inputs.json",
                {
                    "python": platform.python_version(),
                    "httpx": httpx.__version__,
                    "docker": version,
                    "backends": stack.backends,
                    "backend_images": BACKEND_IMAGES,
                    "backend_inputs": BACKEND_IMAGES,
                    "resources": RESOURCES,
                    "policy": policy,
                    "host": host,
                    "personality_sha256": digest(
                        (root / "config/personality.yaml").read_text()
                    ),
                },
            )
            fingerprint = fingerprints(root, BACKEND_IMAGES, policy) | {
                "host": digest(host)
            }
            ordinal = 0
            for index in range(3):
                for role in (
                    ("baseline", "candidate")
                    if index % 2 == 0
                    else ("candidate", "baseline")
                ):
                    ordinal += 1
                    image = reports[role]["image"]["id"]
                    name = stack._create(
                        "core",
                        image,
                        env=environment(stack, f"perf_{role}_{index}"),
                        suffix=f"{role}-{index}",
                        fixture_volume=True,
                    )
                    actual = stack.inspect_container(name, "core")
                    row = {
                        "round": index,
                        "started_at": (
                            started + timedelta(seconds=ordinal + 1)
                        ).isoformat(),
                        "finished_at": (
                            started + timedelta(seconds=ordinal + 1.5)
                        ).isoformat(),
                        "actual_container": actual,
                        "fixture": {
                            "fixture": "klukai-deterministic-v1",
                            "external_calls": 0,
                            "gpu_calls": 0,
                            # One startup MCP handshake per core so far.
                            "counts": {
                                "GET /fixture-stats": ordinal,
                                "POST /mcp": ordinal,
                            },
                        },
                        "routes": [
                            {
                                "method": method,
                                "route": route,
                                "warmup_count": 10,
                                "samples": [
                                    {
                                        "status": status,
                                        "contract_ok": True,
                                        "elapsed_ms": millis
                                        if role == "candidate"
                                        else 10,
                                    }
                                    for _ in range(100)
                                ],
                            }
                            for method, route, status in TARGETS
                        ],
                    }
                    reports[role]["rounds"].append(row)
                    write_exclusive(output / f"{role}-round-{index}.json", row)
                    stack.stop_core(name)
            stack.close()
        for role, report in reports.items():
            report["provenance"] = fingerprint
            write_exclusive(output / (role + ".json"), report)
        gate = compare(
            reports["baseline"],
            reports["candidate"],
            policy=policy,
            baseline_image=reports["baseline"]["image"]["input"],
            candidate_image=reports["candidate"]["image"]["input"],
            baseline_revision=baseline,
            candidate_revision=revision,
        )
        write_exclusive(output / "gate.json", gate)
        write_exclusive(
            output / "manifest.json",
            {
                "schema_version": 1,
                "source_revision": revision,
                "source": captured,
                "producer_sha256": helpers(root),
                "started_at": started.isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
                "passed": True,
                "certifying": True,
                "development": False,
                "source_unchanged": True,
            },
        )
        rehash(output)
    finally:
        ACTIVE.reset(token)
    return root, output, revision
