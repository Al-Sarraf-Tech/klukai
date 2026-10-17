"""Rehashed Docker lifecycle tampering and private producer failure evidence."""

from pathlib import Path
import json
import subprocess

import pytest


def rewrite_transcript(output, mutate):
    path = output / "docker-commands.json"
    rows = json.loads(path.read_text())
    for row in rows:
        for stream in ("stdout", "stderr"):
            row[stream + "_bytes"] = (output / row[stream]).read_text()
    mutate(rows)
    for file in output.glob("docker-*.*"):
        if file != path:
            file.unlink()
    for index, row in enumerate(rows):
        for stream in ("stdout", "stderr"):
            name = f"docker-{index:03d}.{stream}"
            row[stream] = name
            (output / name).write_text(row.pop(stream + "_bytes"))
    path.write_text(json.dumps(rows) + "\n")


@pytest.mark.parametrize(
    "mutation",
    [
        "orphan",
        "extra",
        "reordered",
        "removedstop",
        "cleanup",
        "image",
        "mount",
        "network",
        "providerenv",
        "inherited",
        "warning",
        "logswarning",
        "cliwarning",
        "exit",
        "timeout",
        "stdin",
        "entrypoint",
        "user",
        "profile",
        "missingabsence",
        "execaftercleanup",
    ],
)
def test_rehashed_docker_transcript_is_consumed_completely(tmp_path, mutation):
    from perf_evidence_fixture import bundle, rehash
    from tools.ci.perf_evidence import validate

    root, output, revision = bundle(tmp_path)

    def change(rows):
        stop = next(i for i, row in enumerate(rows) if row["argv"][1] == "stop")
        create = next(i for i, row in enumerate(rows) if row["argv"][1] == "create")
        inspect = next(
            i
            for i, row in enumerate(rows)
            if row["argv"][1] == "inspect" and "-postgres" == row["argv"][-1][-9:]
        )
        if mutation in {"extra", "execaftercleanup"}:
            from copy import deepcopy

            extra = deepcopy(rows[0])
            extra["argv"] = ["docker", "exec", "removed-container", "true"]
            rows.append(extra)
        elif mutation == "orphan":
            rows.pop()
        elif mutation == "reordered":
            rows[stop], rows[stop + 1] = rows[stop + 1], rows[stop]
        elif mutation == "removedstop":
            rows.pop(stop)
        elif mutation == "cleanup":
            rows[-2]["exit_code"] = 0
        elif mutation == "missingabsence":
            rows[-2]["stderr_bytes"] = "unrelated failure\n"
        elif mutation in {"image", "mount", "network", "providerenv"}:
            rows[create]["argv"].extend(
                ["--mount", "type=bind,source=/home,target=/unsafe"]
                if mutation == "mount"
                else ["--network", "host"]
                if mutation == "network"
                else ["--env", "ANTHROPIC_API_KEY"]
                if mutation == "providerenv"
                else ["sha256:" + "f" * 64]
            )
        elif mutation == "inherited":
            data = json.loads(rows[inspect]["stdout_bytes"])
            data["Config"]["Env"].append({"key": "ANTHROPIC_API_KEY", "nonempty": True})
            rows[inspect]["stdout_bytes"] = json.dumps(data)
        elif mutation in {"entrypoint", "user", "profile"}:
            data = json.loads(rows[inspect]["stdout_bytes"])
            if mutation == "profile":
                data["HostConfig"]["Privileged"] = True
            else:
                data["Config"]["User" if mutation == "user" else "Entrypoint"] = (
                    "0:0" if mutation == "user" else ["/bin/sh"]
                )
            rows[inspect]["stdout_bytes"] = json.dumps(data)
        elif mutation == "warning":
            rows[stop]["stderr_bytes"] = "RuntimeWarning: hidden issue\n"
        elif mutation == "logswarning":
            logs = next(i for i, row in enumerate(rows) if row["argv"][1] == "logs")
            rows[logs]["stderr_bytes"] = "x.py:1: DeprecationWarning: hidden\n"
        elif mutation == "cliwarning":
            rows[create]["stderr_bytes"] = (
                "WARNING: Your kernel does not support memory limit\n"
            )
        elif mutation == "exit":
            rows[stop]["exit_code"] = 1
        elif mutation == "timeout":
            rows[stop]["exit_code"] = None
            rows[stop]["error_type"] = "TimeoutExpired"
        elif mutation == "stdin":
            rows[stop]["stdin"] = "unexpected"

    rewrite_transcript(output, change)
    rehash(output)
    with pytest.raises(ValueError):
        validate(root, output, revision, 1)


def test_producer_rejects_unproven_cleanup_and_retains_safe_failure(
    tmp_path, monkeypatch
):
    from tools.ci.perf_recording import ACTIVE, Recorder
    from tools.ci.perf_resources import DisposableStack

    def boundary(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            1 if argv[1] == "inspect" else 0,
            "",
            "permission denied" if argv[1] == "inspect" else "",
        )

    monkeypatch.setattr(subprocess, "run", boundary)
    recorder = Recorder(tmp_path)
    token = ACTIVE.set(recorder)
    try:
        stack = DisposableStack(tmp_path, Path.cwd())
        stack.containers.append(stack.prefix + "-fixture")
        with pytest.raises(RuntimeError, match="cleanup"):
            stack.close()
    finally:
        ACTIVE.reset(token)
    assert json.loads((tmp_path / "cleanup.json").read_text())["verified"] is False
    assert "permission denied" in "".join(
        p.read_text() for p in tmp_path.glob("docker-*.stderr")
    )


def test_recorder_omits_owner_environment_and_scrubs_fresh_dsn(tmp_path, monkeypatch):
    from tools.ci.perf_recording import Recorder

    monkeypatch.setenv("ANTHROPIC_API_KEY", "owner-secret-never-inherited")

    def boundary(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, json.dumps(kwargs["env"]), "")

    monkeypatch.setattr(subprocess, "run", boundary)
    recorder = Recorder(tmp_path)
    recorder.secrets.append("fresh-ephemeral")
    recorder.run(
        "version",
        environment={"DATABASE_URL": "postgresql://perf:fresh-ephemeral@postgres/test"},
    )
    retained = "".join(p.read_text() for p in tmp_path.iterdir() if p.is_file())
    assert "owner-secret-never-inherited" not in retained
    assert "ANTHROPIC_API_KEY" not in retained
    assert "fresh-ephemeral" not in retained
    assert "postgresql://perf:<disposable-credential>@postgres/test" in retained


def test_effective_security_option_normalization_is_valid(tmp_path):
    from perf_evidence_fixture import bundle, rehash
    from tools.ci.perf_evidence import validate

    root, output, revision = bundle(tmp_path)

    def normalize(rows):
        for row in rows:
            if row["argv"][1] == "inspect" and row["exit_code"] == 0:
                data = json.loads(row["stdout_bytes"])
                data["HostConfig"]["SecurityOpt"] = ["no-new-privileges:true"]
                row["stdout_bytes"] = json.dumps(data)

    rewrite_transcript(output, normalize)
    rehash(output)
    assert validate(root, output, revision, 1)["certifying"] is True


def test_sensitive_image_projection_retains_safe_failure_facts(tmp_path, monkeypatch):
    from tools.ci.perf_recording import ACTIVE, Recorder
    from tools.ci.perf_resources import inspect_image

    info = {
        "Id": "sha256:" + "a" * 64,
        "Os": "linux",
        "Architecture": "amd64",
        "Config": {"Env": [{"key": "ANTHROPIC_API_KEY", "nonempty": True}]},
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, json.dumps(info), ""
        ),
    )
    recorder = Recorder(tmp_path)
    token = ACTIVE.set(recorder)
    try:
        with pytest.raises(ValueError, match="baked credentials"):
            inspect_image("sha256:" + "a" * 64)
    finally:
        ACTIVE.reset(token)
    retained = json.loads((tmp_path / "docker-000.stdout").read_text())
    assert retained["Config"]["Env"] == [{"key": "ANTHROPIC_API_KEY", "nonempty": True}]


def test_same_approved_backend_id_accepts_retained_requested_alias(tmp_path):
    from perf_evidence_fixture import bundle, rehash
    from tools.ci.perf_evidence import validate
    from tools.ci.perf_resources import BACKEND_IMAGES

    root, output, revision = bundle(tmp_path)
    alias = "cached.example.invalid/valkey@sha256:" + "d" * 64
    inputs = json.loads((output / "inputs.json").read_text())
    inputs["backend_inputs"] = dict(BACKEND_IMAGES) | {"redis": alias}
    (output / "inputs.json").write_text(json.dumps(inputs))

    def change(rows):
        for row in rows:
            if (
                row["argv"][1:3] == ["image", "inspect"]
                and row["argv"][-1] == BACKEND_IMAGES["redis"]
            ):
                row["argv"][-1] = alias

    rewrite_transcript(output, change)
    rehash(output)
    assert validate(root, output, revision, 1)["certifying"] is True


def override_boundary(tmp_path, monkeypatch, *, alias=False):
    from perf_evidence_fixture import bundle
    from tools.ci.perf_recording import Recorder
    from tools.ci.perf_resources import BACKEND_IMAGES, DisposableStack

    _, output, _ = bundle(tmp_path)
    rows = json.loads((output / "docker-commands.json").read_text())
    images = {
        row["argv"][-1]: json.loads((output / row["stdout"]).read_text())
        for row in rows
        if row["argv"][1:3] == ["image", "inspect"]
    }
    default = dict(BACKEND_IMAGES)
    unknown = (
        "cached.example.invalid/valkey@sha256:" + "d" * 64
        if alias
        else "sha256:" + "f" * 64
    )
    monkeypatch.setenv("KLUKAI_PERF_REDIS_IMAGE", unknown)

    def boundary(argv, **kwargs):
        if argv[1:3] == ["context", "inspect"]:
            return subprocess.CompletedProcess(
                argv, 0, "unix:///var/run/docker.sock", ""
            )
        if argv[1:3] == ["image", "inspect"]:
            from copy import deepcopy

            info = deepcopy(
                images[default["redis"] if argv[-1] == unknown else argv[-1]]
            )
            info["Id"] = default["redis"] if alias and argv[-1] == unknown else argv[-1]
            return subprocess.CompletedProcess(argv, 0, json.dumps(info), "")
        return subprocess.CompletedProcess(
            argv, 1, "", "injected network-create failure"
        )

    monkeypatch.setattr(subprocess, "run", boundary)
    target = tmp_path / "admission"
    target.mkdir(mode=0o700)
    return Recorder(target), DisposableStack(target, Path.cwd()), default


def test_different_backend_id_rejects_before_any_mutation(tmp_path, monkeypatch):
    from tools.ci.perf_recording import ACTIVE

    recorder, stack, _ = override_boundary(tmp_path, monkeypatch)
    token = ACTIVE.set(recorder)
    try:
        with pytest.raises(ValueError, match="reviewed backend"):
            stack.start()
    finally:
        ACTIVE.reset(token)
    assert all(row["argv"][1] in {"image", "context"} for row in recorder.rows)


def test_backend_admission_failure_never_changes_next_run_defaults(
    tmp_path, monkeypatch
):
    from tools.ci.perf_recording import ACTIVE
    from tools.ci.perf_resources import BACKEND_IMAGES

    recorder, stack, default = override_boundary(tmp_path, monkeypatch)
    token = ACTIVE.set(recorder)
    try:
        with pytest.raises((ValueError, RuntimeError)):
            stack.start()
    finally:
        ACTIVE.reset(token)
    assert BACKEND_IMAGES == default


def test_development_keeps_immutable_override_utility(tmp_path, monkeypatch):
    from tools.ci.perf_recording import ACTIVE
    from tools.ci.perf_resources import BACKEND_IMAGES

    recorder, stack, default = override_boundary(tmp_path, monkeypatch)
    token = ACTIVE.set(recorder)
    try:
        with pytest.raises(RuntimeError, match="Disposable Docker command failed"):
            stack.start(certifying=False)
    finally:
        ACTIVE.reset(token)
    assert recorder.rows[-1]["argv"][1:3] == ["network", "create"]
    assert BACKEND_IMAGES == default


def test_same_id_alias_passes_producer_admission_before_injected_network_failure(
    tmp_path, monkeypatch
):
    from tools.ci.perf_recording import ACTIVE

    recorder, stack, default = override_boundary(tmp_path, monkeypatch, alias=True)
    token = ACTIVE.set(recorder)
    try:
        with pytest.raises(RuntimeError, match="Disposable Docker command failed"):
            stack.start()
    finally:
        ACTIVE.reset(token)
    assert recorder.rows[-1]["argv"][1:3] == ["network", "create"]
    assert stack.backend_images == default
    assert stack.backend_inputs["redis"].startswith("cached.example.invalid/")


def test_complete_development_bundle_never_certifies(tmp_path):
    from perf_evidence_fixture import bundle, rehash
    from tools.ci.perf_evidence import validate

    root, output, revision = bundle(tmp_path)
    manifest = json.loads((output / "manifest.json").read_text())
    manifest.update(development=True, certifying=False)
    (output / "manifest.json").write_text(json.dumps(manifest))
    for role in ("baseline", "candidate"):
        path = output / (role + ".json")
        data = json.loads(path.read_text())
        data["development_only"] = True
        path.write_text(json.dumps(data))
    rehash(output)
    with pytest.raises(ValueError, match="Noncertifying"):
        validate(root, output, revision, 1)


def test_reordered_input_map_cannot_redefine_producer_command_order(tmp_path):
    from perf_evidence_fixture import bundle, rehash
    from tools.ci.perf_evidence import validate

    root, output, revision = bundle(tmp_path)
    inputs = json.loads((output / "inputs.json").read_text())
    inputs["backend_images"] = dict(reversed(list(inputs["backend_images"].items())))
    (output / "inputs.json").write_text(json.dumps(inputs))

    def change(rows):
        indices = [
            i for i, row in enumerate(rows) if row["argv"][1:3] == ["image", "inspect"]
        ][2:]
        values = [rows[i] for i in reversed(indices)]
        for index, row in zip(indices, values, strict=True):
            rows[index] = row

    rewrite_transcript(output, change)
    rehash(output)
    with pytest.raises(ValueError):
        validate(root, output, revision, 1)


def test_container_log_levels_are_not_python_warnings(tmp_path):
    from perf_evidence_fixture import bundle, rehash
    from tools.ci.perf_evidence import validate

    root, output, revision = bundle(tmp_path)

    def change(rows):
        for row in rows:
            if row["argv"][1] == "logs":
                row["stderr_bytes"] = (
                    "app.auth WARNING Skipping user miguel: no seed password\n"
                    "WARN qdrant: tmpfs storage\n"
                    "warning: directory '/data' is not writable\n"
                )

    rewrite_transcript(output, change)
    rows = json.loads((output / "docker-commands.json").read_text())
    for row in rows:
        retained = output / (row["argv"][-1] + ".log")
        if row["argv"][1] == "logs" and retained.exists():
            retained.write_text(
                (output / row["stdout"]).read_text()
                + (output / row["stderr"]).read_text()
            )
    rehash(output)
    assert validate(root, output, revision, 1)["certifying"] is True


def test_newer_docker_engine_inspect_spellings_still_certify(tmp_path):
    """containerd store + Docker 29: CAP_ prefixes, omitted User, no stopped IP."""
    from perf_evidence_fixture import bundle, rehash
    from tools.ci.perf_evidence import validate

    root, output, revision = bundle(tmp_path)

    def change(rows):
        for row in rows:
            if row["argv"][1] not in {"inspect", "image"} or row["exit_code"]:
                continue
            data = json.loads(row["stdout_bytes"])
            if not isinstance(data, dict) or "Config" not in data:
                continue
            if row["argv"][1] == "image" and data["Config"].get("User") == "":
                data["Config"]["User"] = None
            host = data.get("HostConfig") or {}
            if host.get("CapAdd"):
                host["CapAdd"] = ["CAP_" + cap for cap in host["CapAdd"]]
            if data.get("State", {}).get("Running") is False:
                for network in data["NetworkSettings"]["Networks"].values():
                    network["IPAddress"] = ""
            row["stdout_bytes"] = json.dumps(data)

    rewrite_transcript(output, change)
    rehash(output)
    assert validate(root, output, revision, 1)["certifying"] is True
