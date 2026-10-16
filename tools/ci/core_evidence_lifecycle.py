"""Reconstruct actual full-core container lifecycle from retained Docker output."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import re
from typing import Any

from .core_evidence_identity import document, require
from .core_profile import (
    DRIVER,
    core_flags,
    postgres_flags,
    safe_environment,
    validate_core_profile,
    validate_postgres_profile,
)
from .core_resources import CONTAINER_VIEW, LABEL


def validate_creation(
    argv: list[str],
    tail: list[str],
    flags: list[str],
    info: dict[str, Any],
    expected: dict[str, str],
) -> None:
    values = info["Config"]["Env"]
    names = [value.partition("=")[0] for value in values]
    require(len(names) == len(set(names)), "Duplicate actual environment key")
    inherited = ["--env=" + key + "=" for key in sorted(set(names) - expected.keys())]
    supplied = ["--env=" + key for key in sorted(expected)]
    require(
        argv[5:] == [*flags, *inherited, *supplied, *tail],
        "Actual create flags/environment/entrypoint differ",
    )


def matching(rows: list[dict[str, Any]], argv: list[str]) -> list[int]:
    return [i for i, row in enumerate(rows) if row["argv"] == ["docker", *argv]]


def one(rows: list[dict[str, Any]], argv: list[str]) -> int:
    indices = matching(rows, argv)
    require(len(indices) == 1, "Missing/duplicate lifecycle command: " + argv[0])
    return indices[0]


def validate_sequence(
    report: dict[str, Any], rows: list[dict[str, Any]], state: dict[str, Any]
) -> None:
    """Consume every actual producer call, including its precise recovery paths."""
    position = 0

    def take(
        argv: list[str], *, success: bool = True, timeout: float = 30
    ) -> dict[str, Any]:
        nonlocal position
        require(
            position < len(rows) and rows[position]["argv"] == ["docker", *argv],
            f"Unexpected producer command sequence at {position}",
        )
        row = rows[position]
        position += 1
        require(row["timeout_seconds"] <= timeout, "Producer command timeout differs")
        if success:
            require(
                row["exit_code"] == 0 and "error_type" not in row,
                "Producer command failed",
            )
        return row

    def inspect(identity: str, *, success: bool = True) -> dict[str, Any]:
        return take(
            ["inspect", "--type=container", "--format=" + CONTAINER_VIEW, identity],
            success=success,
        )

    def info(row: dict[str, Any]) -> dict[str, Any]:
        data = json.loads(row["stdout_text"])
        return data[0] if isinstance(data, list) else data

    for role in ("core", "postgres"):
        take(["image", "inspect", report["images"][role]["input"]])
    take(["version", "--format={{json .}}"])
    network = state["network"]
    name, token = network["name"], state["token"]
    take(["network", "create", "--internal", "--label=" + LABEL + "=" + token, name])
    take(["network", "inspect", name])
    pg, core = state["containers"]
    for resource in (pg, core):
        created_argv = next(
            row["argv"]
            for row in rows
            if row["argv"][1:3] == ["create", "--name=" + resource["name"]]
        )
        take(created_argv[1:])
        if resource is pg:
            take(["start", pg["id"]])
            readiness = [
                "exec",
                pg["id"],
                "pg_isready",
                "-h",
                "postgres",
                "-U",
                "core_test",
                "-d",
                "core_test",
            ]
            for attempt in range(200):
                ready = take(readiness, success=False)
                if ready["exit_code"] == 0:
                    break
            else:
                require(False, "PostgreSQL readiness exceeds producer60second bound")
            duration = (
                datetime.fromisoformat(report["finished_at"])
                - datetime.fromisoformat(report["started_at"])
            ).total_seconds()
            require(
                duration >= 0.3 * attempt,
                "Readiness retry sleep exceeds recorded producer duration",
            )
            take(
                [
                    "exec",
                    "--env=PGPASSWORD",
                    pg["id"],
                    "psql",
                    "-h",
                    "postgres",
                    "-U",
                    "core_test",
                    "-d",
                    "core_test",
                    "-Atc",
                    "SELECT current_setting('server_version_num')",
                ]
            )
            take(["exec", pg["id"], "id", "-u"])
            inspect(pg["id"])
        else:
            inspect(core["id"])
            take(["start", "--attach", core["id"]], timeout=1800)
            inspect(core["id"])
    for resource in (core, pg):
        owned = inspect(resource["name"], success=False)
        if owned["exit_code"] != 0:
            continue  # Exact absence evidence is independently checked below.
        if info(owned)["State"]["Running"]:
            require(resource is pg, "Exited core cannot restart during cleanup")
            stopped = take(
                ["stop", "--time=3", resource["id"]], success=False, timeout=15
            )
            if stopped["exit_code"] != 0:
                take(["kill", resource["id"]])
        take(["wait", resource["id"]], timeout=20)
        inspect(resource["name"])
        take(["rm", "--volumes", resource["id"]])
        inspect(resource["name"], success=False)
    final_network = take(["network", "inspect", name])
    require(
        not json.loads(final_network["stdout_text"])[0]["Containers"],
        "Final network still has endpoints",
    )
    take(["network", "rm", network["id"]])
    take(["network", "inspect", name], success=False)
    require(position == len(rows), "Unconsumed producer command sequence")


def lifecycle(output: Path, report: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    state = document(output / "resources.json")
    token = state["token"]
    require(
        type(token) is str and re.fullmatch(r"[a-f0-9]{32}", token),
        "Invalid resource token",
    )
    network = state["network"]
    name = "klukai-core-ci-" + token
    require(
        network["name"] == name and re.fullmatch(r"[a-f0-9]{64}", network["id"]),
        "Network identity differs",
    )
    require(
        state["cleaned"] is True
        and state["cleanup_errors"] == []
        and network["removed"] is True,
        "Cleanup did not succeed",
    )
    created = one(
        rows,
        ["network", "create", "--internal", "--label=" + LABEL + "=" + token, name],
    )
    require(
        rows[created]["exit_code"] == 0
        and rows[created]["stdout_text"].strip() == network["id"],
        "Network creation failed",
    )
    network_inspects = matching(rows, ["network", "inspect", name])
    require(
        len(network_inspects) >= 3 and created < network_inspects[0],
        "Missing network lifecycle",
    )
    for i in network_inspects[:-1]:
        row = rows[i]
        require(row["exit_code"] == 0, "Network inspect failed")
        infos = json.loads(row["stdout_text"])
        require(type(infos) is list and len(infos) == 1, "Invalid network inspect")
        info = infos[0]
        require(
            info["Id"] == network["id"]
            and info["Labels"].get(LABEL) == token
            and info["Internal"] is True,
            "Actual network ownership/isolation differs",
        )
    removed = one(rows, ["network", "rm", network["id"]])
    last = rows[network_inspects[-1]]
    require(
        removed < network_inspects[-1]
        and rows[removed]["exit_code"] == 0
        and last["exit_code"] != 0,
        "Network removal failed",
    )
    require(
        re.search(
            r"(?:No such network: "
            + re.escape(name)
            + r"|network "
            + re.escape(name)
            + r" not found)(?:\s|$)",
            last["stderr_text"],
        ),
        "Network absence unproven",
    )
    containers = state["containers"]
    require(
        type(containers) is list
        and [c["role"] for c in containers] == ["postgres", "core"],
        "Full core/PG inventory differs",
    )
    require(
        sum(row["argv"][1:2] == ["create"] for row in rows) == 2,
        "Extra/missing container creation",
    )
    require(
        type(report["execution"]) is dict
        and all(type(value) is int for value in report["execution"].values()),
        "Invalid real execution exit types",
    )
    env = safe_environment("<disposable-credential>")
    require(report["environment"] == env, "Closed producer environment differs")
    for resource in containers:
        role, cid = resource["role"], resource["id"]
        require(
            re.fullmatch(r"[a-f0-9]{64}", cid)
            and resource["name"] == name + "-" + role,
            "Container identity differs",
        )
        image = report["images"][role if role == "core" else "postgres"]["id"]
        require(
            resource["image"] == image and resource["removed"] is True,
            "Container cleanup identity differs",
        )
        create = [
            i
            for i, row in enumerate(rows)
            if row["argv"][1:3] == ["create", "--name=" + resource["name"]]
        ]
        require(len(create) == 1 and created < create[0], "Container creation unproven")
        initial = rows[create[0]]
        require(
            initial["exit_code"] == 0 and initial["stdout_text"].strip() == cid,
            "Container create failed",
        )
        argv = initial["argv"]
        require(
            argv[3:5] == ["--label=" + LABEL + "=" + token, "--pull=never"],
            "Creation ownership differs",
        )
        tail = [image, "-W", "error", "-c", DRIVER] if role == "core" else [image]
        require(argv[-len(tail) :] == tail, "Actual core driver/image differs")
        inspected: list[tuple[int, dict[str, Any]]] = []
        for i, row in enumerate(rows):
            if row["argv"] in (
                [
                    "docker",
                    "inspect",
                    "--type=container",
                    "--format=" + CONTAINER_VIEW,
                    cid,
                ],
                [
                    "docker",
                    "inspect",
                    "--type=container",
                    "--format=" + CONTAINER_VIEW,
                    resource["name"],
                ],
            ):
                if row["exit_code"] == 0:
                    info = json.loads(row["stdout_text"])
                    if isinstance(info, list) and len(info) == 1:
                        info = info[0]
                    require(
                        info["Id"] == cid
                        and info["Image"] == image
                        and info["Config"]["Image"] == image
                        and info["Config"]["Labels"].get(LABEL) == token,
                        "Actual container ownership/image differs",
                    )
                    require(
                        type(info["State"].get("Running")) is bool
                        and type(info["State"].get("ExitCode")) is int,
                        "Invalid actual container state types",
                    )
                    inspected.append((i, info))
        require(
            inspected and create[0] < inspected[0][0],
            "Missing actual container profile",
        )
        info = inspected[0][1]
        if role == "core":
            validate_core_profile(
                info, output / "source", output / "results", name, env
            )
            flags = core_flags(output / "source", output / "results", name)
            validate_creation(argv, tail, flags, info, env)
            attached = one(rows, ["start", "--attach", cid])
            require(
                not any(
                    re.search(
                        r"\b\w*Warning\b|coroutine .*never awaited|Exception ignored",
                        rows[attached][stream + "_text"],
                        re.I,
                    )
                    for stream in ("stdout", "stderr")
                ),
                "Unexpected producer attach warning",
            )
            require(inspected[0][0] < attached, "Core attach ordering differs")
            terminal = [(i, data) for i, data in inspected if i > attached]
            require(
                terminal
                and terminal[0][1]["State"].get("Running") is False
                and type(terminal[0][1]["State"].get("ExitCode")) is int
                and terminal[0][1]["State"]["ExitCode"] == 0,
                "Actual core execution failed",
            )
            require(
                rows[attached]["exit_code"] == 0
                and report["execution"]
                == {"attach_exit_code": 0, "container_exit_code": 0},
                "Attach execution differs",
            )
        else:
            pg_env = {
                "POSTGRES_USER": "core_test",
                "POSTGRES_DB": "core_test",
                "POSTGRES_PASSWORD": "<disposable-credential>",
                "PGDATA": "/var/lib/postgresql/data/pgdata",
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "PG_MAJOR": "17",
                "LANG": "en_US.utf8",
            }
            validate_postgres_profile(info, name, pg_env)
            validate_creation(argv, tail, postgres_flags(name), info, pg_env)
            started = one(rows, ["start", cid])
            require(
                create[0] < started < inspected[0][0]
                and rows[started]["exit_code"] == 0,
                "PostgreSQL start failed",
            )
            version = one(
                rows,
                [
                    "exec",
                    "--env=PGPASSWORD",
                    cid,
                    "psql",
                    "-h",
                    "postgres",
                    "-U",
                    "core_test",
                    "-d",
                    "core_test",
                    "-Atc",
                    "SELECT current_setting('server_version_num')",
                ],
            )
            actual = rows[version]["stdout_text"].strip()
            require(
                rows[version]["exit_code"] == 0
                and actual == report["postgres_server_version_num"]
                and actual.isdigit()
                and 170000 <= int(actual) < 180000,
                "Actual PostgreSQL major differs",
            )
            uid = one(rows, ["exec", cid, "id", "-u"])
            require(
                rows[uid]["exit_code"] == 0
                and rows[uid]["stdout_text"].strip().isdigit()
                and int(rows[uid]["stdout_text"].strip()) != 0,
                "Actual PostgreSQL root user",
            )
        if not matching(rows, ["wait", cid]):
            absent = [
                row
                for row in rows
                if row["argv"]
                == [
                    "docker",
                    "inspect",
                    "--type=container",
                    "--format=" + CONTAINER_VIEW,
                    resource["name"],
                ]
            ]
            require(
                len(absent) == 1
                and absent[0]["exit_code"] != 0
                and re.search(
                    r"No such (?:object|container): "
                    + re.escape(resource["name"])
                    + r"(?:\s|$)",
                    absent[0]["stderr_text"],
                )
                and not any(
                    key in resource for key in ("wait_exit_code", "actual_exit_code")
                ),
                "Early cleanup absence unproven",
            )
            continue
        waited = one(rows, ["wait", cid])
        removed_container = one(rows, ["rm", "--volumes", cid])
        require(
            inspected[0][0] < waited < removed_container < removed
            and rows[waited]["exit_code"] == rows[removed_container]["exit_code"] == 0,
            "Container cleanup order/exit differs",
        )
        wait_exit = int(rows[waited]["stdout_text"].strip())
        require(
            type(resource["actual_exit_code"]) is int
            and type(resource["wait_exit_code"]) is int
            and resource["actual_exit_code"] == resource["wait_exit_code"] == wait_exit,
            "Wait/actual cleanup exit differs",
        )
        final = [(i, data) for i, data in inspected if waited < i < removed_container]
        require(
            final
            and final[-1][1]["State"].get("Running") is False
            and final[-1][1]["State"].get("ExitCode") == wait_exit,
            "Actual final state differs",
        )
        absent = [
            row
            for row in rows[removed_container + 1 : removed]
            if row["argv"]
            == [
                "docker",
                "inspect",
                "--type=container",
                "--format=" + CONTAINER_VIEW,
                resource["name"],
            ]
        ]
        require(
            len(absent) == 1
            and absent[0]["exit_code"] != 0
            and re.search(
                r"No such (?:object|container): "
                + re.escape(resource["name"])
                + r"(?:\s|$)",
                absent[0]["stderr_text"],
            ),
            "Container absence unproven",
        )
        if role == "core":
            require(wait_exit == 0, "Core cleanup exit failed")
    validate_sequence(report, rows, state)
