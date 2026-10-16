"""Synthetic Docker metadata around genuine tiny executed unit measurements."""

from __future__ import annotations
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
from contextlib import closing

from tools.ci import core_runner as runner, core_source as source
from tools.ci.core_profile import coverage_database
from tools.ci.core_results import coverage_results, junit_results
from test_core_runner import valid_core_info


def bundle(tmp_path, observed, monkeypatch):
    measured, coverage = observed
    root = tmp_path / "repo"
    root.mkdir()
    core = root / "docker/core"
    core.mkdir(parents=True)
    shutil.copytree(
        measured / "app", core / "app", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copyfile(measured / ".coveragerc", core / ".coveragerc")
    (core / "tests").mkdir()
    shutil.copyfile(measured / "tests/test_domain.py", core / "tests/test_domain.py")
    actual = Path(runner.__file__).parents[2]
    (root / "tools/ci").mkdir(parents=True)
    for name in [
        "core_runner",
        "core_source",
        "core_resources",
        "core_profile",
        "python_executor",
        "cancellation",
        "core_results",
        "core_evidence",
        "core_evidence_identity",
        "core_evidence_lifecycle",
    ]:
        shutil.copyfile(actual / f"tools/ci/{name}.py", root / f"tools/ci/{name}.py")
    shutil.copyfile(
        actual / "docker/core/requirements-ci.txt", core / "requirements-ci.txt"
    )
    for args in [
        ["init", "-q"],
        ["config", "user.name", "Unit fixture"],
        ["config", "user.email", "unit@example.invalid"],
        ["config", "commit.gpgsign", "false"],
        ["config", "core.hooksPath", "/dev/null"],
        ["add", "."],
        ["commit", "-qm", "synthetic metadata fixture"],
    ]:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    revision = source.git(root, "rev-parse", "HEAD")
    output = tmp_path / "bundle"
    image = "sha256:" + "1" * 64
    pg = "sha256:" + "2" * 64
    containers = {}
    network = {}
    original_run = subprocess.run

    def docker(argv, **kwargs):
        if argv[0] != "docker":
            return original_run(argv, **kwargs)
        args = argv[1:]
        text = ""
        err = ""
        code = 0
        if args[:2] == ["image", "inspect"]:
            config = {
                "User": "1000:1000",
                "Entrypoint": ["/opt/ci/bin/python"],
                "Labels": {
                    "org.opencontainers.image.revision": revision,
                    "cc.appnest.klukai.ci-lock-sha256": source.sha256(
                        core / "requirements-ci.txt"
                    ),
                },
                "Env": ["PG_MAJOR=17"],
            }
            text = json.dumps(
                [
                    {
                        "Id": args[-1],
                        "Architecture": "amd64",
                        "Os": "linux",
                        "RepoDigests": [],
                        "Config": config,
                    }
                ]
            )
        elif args[0] == "version":
            text = "{}"
        elif args[:2] == ["network", "create"]:
            token = args[-1].removeprefix("klukai-core-ci-")
            network.update(
                Id="4" * 64,
                Internal=True,
                Labels={"cc.appnest.klukai.core-run": token},
                Containers={},
            )
            text = network["Id"]
        elif args[:2] == ["network", "inspect"]:
            if network:
                text = json.dumps([network])
            else:
                code = 1
                err = "network " + args[-1] + " not found"
        elif args[:2] == ["network", "rm"]:
            network.clear()
        elif args[0] == "create":
            resource = json.loads((output / "resources.json").read_text())[
                "containers"
            ][-1]
            role = resource["role"]
            cid = ("5" if role == "postgres" else "6") * 64
            info = valid_core_info(output)
            info.update(
                Id=cid, Image=resource["image"], State={"Running": False, "ExitCode": 0}
            )
            info["Config"].update(
                Image=resource["image"],
                Labels={
                    "cc.appnest.klukai.core-run": network["Labels"][
                        "cc.appnest.klukai.core-run"
                    ]
                },
                Env=[
                    k + "=" + v
                    for k, v in kwargs["env"].items()
                    if k not in {"DOCKER_CONFIG", "LC_ALL"}
                ],
            )
            allowed = (
                runner.safe_environment("unused")
                if role == "core"
                else {
                    k: kwargs["env"][k]
                    for k in [
                        "POSTGRES_USER",
                        "POSTGRES_DB",
                        "POSTGRES_PASSWORD",
                        "PGDATA",
                        "PATH",
                        "PG_MAJOR",
                        "LANG",
                    ]
                }
            )
            info["Config"]["Env"] = [k + "=" + kwargs["env"][k] for k in allowed]
            for arg in args:
                if arg.startswith("--env=") and arg.endswith("="):
                    info["Config"]["Env"].append(arg.removeprefix("--env="))
            info["HostConfig"]["NetworkMode"] = resource["name"].rsplit("-", 1)[0]
            info["NetworkSettings"]["Networks"] = {
                info["HostConfig"]["NetworkMode"]: {"Gateway": "", "IPv6Gateway": ""}
            }
            if role == "postgres":
                info["Config"]["User"] = "postgres"
                info["Mounts"] = []
                info["HostConfig"].update(
                    NanoCpus=500_000_000,
                    Memory=384 * 1024**2,
                    MemorySwap=384 * 1024**2,
                    PidsLimit=128,
                    Tmpfs={
                        p: "rw,noexec,nosuid,nodev"
                        for p in [
                            "/tmp",
                            "/var/run/postgresql",
                            "/var/lib/postgresql/data",
                        ]
                    },
                )
            containers[resource["name"]] = info
            text = cid
        elif args[0] == "exec":
            text = "170001" if "psql" in args else "1000" if "id" in args else ""
        elif args[0] == "inspect":
            info = next(
                (v for k, v in containers.items() if args[-1] in [k, v["Id"]]), None
            )
            if info:
                text = json.dumps(info)
            else:
                code = 1
                err = "No such object: " + args[-1]
        elif args[0] == "start" and "--attach" in args:
            results = output / "results"
            for name in ["coverage.json", "junit.xml", ".coverage"]:
                shutil.copyfile(measured / name, results / name)
            with closing(sqlite3.connect(results / ".coverage")) as db:
                db.execute(
                    "UPDATE file SET path=REPLACE(path,?,?)",
                    (str(measured), "/source/docker/core"),
                )
                db.commit()
            (results / "coverage-recomputed.json").write_text(json.dumps(coverage))
            (results / "pytest.log").write_text("1 passed in 0.02s\n")
            packages = runner.lock_versions((core / "requirements-ci.txt").read_text())
            (results / "executor-probe.json").write_text(
                json.dumps({"python_version": "3.14.8", "packages": packages})
            )
            result = {
                "passed": True,
                "python_executable": "/opt/ci/bin/python",
                "pytest_exit_code": 0,
                "probe_packages": len(packages),
                "pytest_command": [
                    "/opt/ci/bin/python",
                    "-m",
                    "pytest",
                    "tests/",
                    "-q",
                    "-W",
                    "error",
                    "-o",
                    "cache_dir=/tmp/pytest-cache",
                    "--cov=app",
                    "--cov-branch",
                    "--cov-report=json:/evidence/coverage.json",
                    "--cov-report=",
                    "--junitxml=/evidence/junit.xml",
                ],
                "coverage": coverage_results(core, coverage),
                "junit": junit_results(results / "junit.xml"),
                "retained_coverage": {
                    **coverage_database(results / ".coverage"),
                    "sha256": source.sha256(results / ".coverage"),
                },
            }
            (results / "validation.json").write_text(json.dumps(result))
        elif args[0] == "start":
            pass
        elif args[0] == "wait":
            text = "0"
        elif args[0] == "rm":
            key = next(k for k, v in containers.items() if v["Id"] == args[-1])
            del containers[key]
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(argv, code, text, err)

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", docker)
        report = runner.execute(runner.RunConfig(root, output, revision, image, pg))
    assert report["certifying"], report
    return root, output, revision


def rehash(output):
    report = json.loads((output / "manifest.json").read_text())
    report["raw_sha256"] = {
        n: h
        for n, h in source.inventory(output).items()
        if not n.startswith(("source/", "docker-config/"))
        and n not in {"source.tar", "manifest.json"}
    }
    (output / "manifest.json").write_text(json.dumps(report))
