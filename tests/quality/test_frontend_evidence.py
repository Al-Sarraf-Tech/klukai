"""Frontend CI must prove executed suites and retain revision-bound artifacts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / "tools/ci/frontend_evidence.py"
spec = importlib.util.spec_from_file_location("frontend_evidence", MODULE)
assert spec and spec.loader
frontend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(frontend)
REVISION = "a" * 40


def stream(platform="vm"):
    return [
        {"type": "start", "protocolVersion": "0.1.1"},
        {"type": "suite", "suite": {"id": 0, "platform": platform}},
        {"type": "testStart", "test": {"id": 1, "suiteID": 0}},
        {
            "type": "testDone",
            "testID": 1,
            "result": "success",
            "skipped": False,
            "hidden": False,
        },
        {"type": "done", "success": True},
    ]


def save_stream(path, events):
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def test_machine_counts_only_visible_executed_tests(tmp_path):
    events = stream()
    events[3:3] = [
        {"type": "testStart", "test": {"id": 2, "suiteID": 0}},
        {
            "type": "testDone",
            "testID": 2,
            "result": "success",
            "skipped": False,
            "hidden": True,
        },
        {"type": "testStart", "test": {"id": 3, "suiteID": 0}},
        {
            "type": "testDone",
            "testID": 3,
            "result": "success",
            "skipped": True,
            "hidden": False,
        },
    ]
    result = frontend.parse_machine(
        save_stream(tmp_path / "tests.jsonl", events), "vm", 0
    )
    assert result == {"passed": 1, "skipped": 1, "hidden": 1, "terminal_success": True}


@pytest.mark.parametrize(
    "change",
    [
        "empty",
        "malformed",
        "array",
        "failed_exit",
        "error",
        "false_terminal",
        "no_terminal",
        "zero_tests",
        "only_hidden",
        "only_skipped",
        "duplicate_terminal",
        "after_terminal",
        "unfinished_test",
        "unknown_test",
        "duplicate_test",
        "wrong_platform",
        "failed_test",
        "boolean_exit",
        "unknown_event",
        "missing_start",
    ],
)
def test_invalid_machine_stream_cannot_certify_ci(tmp_path, change):
    events, exit_code = stream(), 0
    if change == "empty":
        events = []
    elif change == "malformed":
        path = tmp_path / "tests.jsonl"
        path.write_text("{broken\n")
    elif change == "array":
        events = [[]]
    elif change == "failed_exit":
        exit_code = 1
    elif change == "error":
        events.insert(3, {"type": "error", "testID": 1, "error": "assertion failed"})
    elif change == "false_terminal":
        events[-1]["success"] = False
    elif change == "no_terminal":
        events.pop()
    elif change == "zero_tests":
        events = [events[0], events[-1]]
    elif change in ("only_hidden", "only_skipped"):
        events[3]["hidden" if change == "only_hidden" else "skipped"] = True
    elif change == "duplicate_terminal":
        events.append(events[-1])
    elif change == "after_terminal":
        events.append({"type": "print", "message": "late"})
    elif change == "unfinished_test":
        events.pop(3)
    elif change == "unknown_test":
        events[3]["testID"] = 9
    elif change == "duplicate_test":
        events.insert(4, events[3])
    elif change == "wrong_platform":
        events[1]["suite"]["platform"] = "chrome"
    elif change == "failed_test":
        events[3]["result"] = "failure"
    elif change == "boolean_exit":
        exit_code = False
    elif change == "unknown_event":
        events.insert(1, {"type": "perhaps_success"})
    else:
        events.pop(0)
    if change != "malformed":
        path = save_stream(tmp_path / "tests.jsonl", events)
    with pytest.raises(frontend.InvalidEvidence):
        frontend.parse_machine(path, "vm", exit_code)


def fixture_bundle(output):
    output.mkdir(mode=0o700)
    raw = output / "raw"
    raw.mkdir()
    pwa = output / "pwa"
    pwa.mkdir()
    for name in ("index.html", "main.dart.js", "flutter_bootstrap.js", "login.html"):
        (pwa / name).write_text("fixture " + name)
    commands = []
    versions = {
        "flutter-version": json.dumps(
            {
                "frameworkVersion": "3.44.2",
                "dartSdkVersion": "3.12.2",
                "frameworkRevision": frontend.FLUTTER_REVISION,
            }
        ),
        "dart-version": "Dart SDK version: 3.12.2 (stable)",
        "chrome-version": "Google Chrome 155.0.8059.79",
    }
    for name, required in frontend.COMMAND_FLAGS.items():
        stdout = raw / f"{name}.stdout"
        if name in ("vm", "chrome"):
            save_stream(stdout, stream(name))
        else:
            stdout.write_text(versions.get(name, "successful command\n"))
        stderr = raw / f"{name}.stderr"
        stderr.write_text("")
        commands.append(
            {
                "name": name,
                "argv": ["tool", *required],
                "exit_code": 0,
                "stdout": frontend.file_record(output, stdout),
                "stderr": frontend.file_record(output, stderr),
            }
        )
    source = {
        "revision": REVISION,
        "git_tree": "b" * 40,
        "inputs": {"files": {"flutter_app/lib/main.dart": "c" * 64}, "sha256": ""},
    }
    source["inputs"]["sha256"] = frontend.tree_digest(source["inputs"]["files"])
    tools = {
        "flutter": {"path": "/tools/flutter", "sha256": "d" * 64},
        "dart": {"path": "/tools/dart", "sha256": "e" * 64},
        "chrome": {"path": "/tools/chrome", "sha256": "f" * 64},
    }
    for name, tool in tools.items():
        value = versions[f"{name}-version"]
        tool["identity"] = json.loads(value) if name == "flutter" else value
    manifest = {
        "schema_version": 1,
        "source": source,
        "commands": commands,
        "tools": tools,
        "sdk_provenance": {"kind": "installed", "archive_sha256": None},
        "pwa": frontend.inventory(pwa),
        "suites": {
            name: frontend.parse_machine(raw / f"{name}.stdout", name, 0)
            for name in ("vm", "chrome")
        },
    }
    (output / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def test_retained_bundle_revalidates_raw_streams_and_exact_pwa(tmp_path):
    output = tmp_path / "evidence"
    manifest = fixture_bundle(output)
    assert frontend.validate_bundle(output, REVISION) == manifest


def test_vm_command_uses_flutter_default_tester_platform():
    # Flutter3.44.2's --platform accepts chrome, not vm. Its default tester
    # still emits machine suite events with platform=vm.
    assert frontend.COMMAND_FLAGS["vm"] == ["test", "--no-pub", "--machine"]


@pytest.mark.parametrize("platform", ["vm", "chrome"])
def test_wrong_explicit_flutter_platform_is_rejected(tmp_path, platform):
    output = tmp_path / "evidence"
    manifest = fixture_bundle(output)
    command = next(c for c in manifest["commands"] if c["name"] == platform)
    if "--platform" in command["argv"]:
        command["argv"][command["argv"].index("--platform") + 1] = "vm"
    else:
        command["argv"].extend(["--platform", "vm"])
    (output / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(frontend.InvalidEvidence):
        frontend.validate_bundle(output, REVISION)


@pytest.mark.parametrize(
    "change",
    [
        "revision",
        "source_hash",
        "schema",
        "failed_command",
        "boolean_exit",
        "missing_command",
        "missing_flag",
        "tool_version",
        "missing_tool",
        "raw_tamper",
        "raw_missing",
        "raw_symlink",
        "pwa_tamper",
        "pwa_missing",
        "pwa_extra",
        "pwa_symlink",
        "pwa_traversal",
        "pwa_tree",
        "suite_summary",
        "public_output",
    ],
)
def test_bundle_rejects_incomplete_forged_or_unbound_evidence(tmp_path, change):
    output = tmp_path / "evidence"
    manifest = fixture_bundle(output)
    if change == "revision":
        manifest["source"]["revision"] = "f" * 40
    elif change == "source_hash":
        manifest["source"]["inputs"]["sha256"] = "0" * 64
    elif change == "schema":
        manifest["schema_version"] = 99
    elif change in ("failed_command", "boolean_exit"):
        manifest["commands"][0]["exit_code"] = (
            1 if change == "failed_command" else False
        )
    elif change == "missing_command":
        manifest["commands"].pop()
    elif change == "missing_flag":
        next(c for c in manifest["commands"] if c["name"] == "pub")["argv"].remove(
            "--enforce-lockfile"
        )
    elif change == "tool_version":
        cmd = next(c for c in manifest["commands"] if c["name"] == "flutter-version")
        path = output / cmd["stdout"]["path"]
        data = json.loads(path.read_text())
        data["frameworkVersion"] = "3.99.0"
        path.write_text(json.dumps(data))
        cmd["stdout"] = frontend.file_record(output, path)
    elif change == "missing_tool":
        del manifest["tools"]["chrome"]
    elif change == "raw_tamper":
        (output / "raw/vm.stdout").write_text("different")
    elif change == "raw_missing":
        (output / "raw/vm.stdout").unlink()
    elif change == "raw_symlink":
        outside = tmp_path / "outside"
        outside.write_bytes((output / "raw/vm.stdout").read_bytes())
        (output / "raw/vm.stdout").unlink()
        (output / "raw/vm.stdout").symlink_to(outside)
    elif change == "pwa_tamper":
        (output / "pwa/index.html").write_text("tampered")
    elif change == "pwa_missing":
        (output / "pwa/index.html").unlink()
    elif change == "pwa_extra":
        (output / "pwa/unbound.js").write_text("new script")
    elif change == "pwa_symlink":
        (output / "pwa/link").symlink_to(output / "pwa/index.html")
    elif change == "pwa_traversal":
        manifest["pwa"]["files"]["../outside"] = "a" * 64
    elif change == "pwa_tree":
        manifest["pwa"]["sha256"] = "a" * 64
    elif change == "suite_summary":
        manifest["suites"]["chrome"]["passed"] = 999
    else:
        output.chmod(0o755)
    (output / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(frontend.InvalidEvidence):
        frontend.validate_bundle(output, REVISION)


def test_output_must_be_explicit_absolute_private_and_outside_checkout(tmp_path):
    with pytest.raises(frontend.InvalidEvidence):
        frontend.initialize(tmp_path, Path("relative"), REVISION)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    with pytest.raises(frontend.InvalidEvidence):
        frontend.initialize(checkout, checkout / "evidence", REVISION)


def test_inventory_rejects_symlink_even_when_target_is_inside_tree(tmp_path):
    (tmp_path / "real").write_text("content")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(frontend.InvalidEvidence):
        frontend.inventory(tmp_path)


def test_flutter_blank_separators_are_not_machine_events(tmp_path):
    path = save_stream(tmp_path / "tests.jsonl", stream())
    path.write_text("\n \n" + path.read_text().replace("\n", "\n\n"))
    assert frontend.parse_machine(path, "vm", 0)["passed"] == 1


def test_missing_executable_retains_failure_and_cannot_be_certified(tmp_path):
    output = tmp_path / "evidence"
    output.mkdir(mode=0o700)
    (output / "raw").mkdir()
    assert frontend.run_command(output, "pub", tmp_path, ["/does/not/exist"]) == 127
    result = json.loads((output / "raw/pub.json").read_text())
    assert result["exit_code"] == 127
    assert (output / "raw/pub.stderr").read_text()


def test_known_flutter_vm_process_envelope_is_allowed_only_between_events(tmp_path):
    events = stream()
    events.insert(
        2, [{"event": "test.startedProcess", "params": {"vmServiceUri": None}}]
    )
    assert (
        frontend.parse_machine(save_stream(tmp_path / "vm.jsonl", events), "vm", 0)[
            "passed"
        ]
        == 1
    )


@pytest.mark.parametrize(
    "envelope",
    [
        [],
        [{"event": "unknown", "params": {}}],
        [{"event": "test.startedProcess", "params": {"error": "failure"}}],
        [{"type": "error", "error": "failure"}],
    ],
)
def test_unknown_flutter_envelopes_are_not_silently_dropped(tmp_path, envelope):
    events = stream()
    events.insert(2, envelope)
    with pytest.raises(frontend.InvalidEvidence):
        frontend.parse_machine(save_stream(tmp_path / "vm.jsonl", events), "vm", 0)


@pytest.mark.parametrize(
    "change", ["identity", "sdk_kind", "sdk_hash", "boolean_schema"]
)
def test_tool_and_snapshot_identity_cannot_be_relabelled(tmp_path, change):
    output = tmp_path / "evidence"
    manifest = fixture_bundle(output)
    if change == "identity":
        manifest["tools"]["chrome"]["identity"] = "Google Chrome 1.0.0.0"
    elif change == "sdk_kind":
        manifest["sdk_provenance"]["kind"] = "official-unverified"
    elif change == "sdk_hash":
        manifest["sdk_provenance"] = {"kind": "owner-snapshot", "archive_sha256": None}
    else:
        manifest["schema_version"] = True
    (output / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(frontend.InvalidEvidence):
        frontend.validate_bundle(output, REVISION)


def test_required_frontend_job_has_no_optional_or_billable_escape():
    import yaml

    root = Path(__file__).resolve().parents[2]
    job = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"][
        "frontend"
    ]
    assert job["runs-on"] == ["self-hosted", "Linux", "X64", "klukai-amarillo-heavy"]
    assert "if" not in job
    assert job.get("continue-on-error") is not True
    # The existing heavy guest's runner-owned, private capacity was verified
    # before selecting this path; no optional repository variable is needed.
    assert job["env"]["KLUKAI_CI_ARTIFACT_ROOT"] == "/opt/klukai-ci/artifacts"
    assert all(
        step.get("continue-on-error") is not True and "if" not in step
        for step in job["steps"]
    )
    assert all("upload-artifact" not in step.get("uses", "") for step in job["steps"])
    assert job["steps"][-1]["run"].strip().endswith("scripts/ci/frontend.sh")
