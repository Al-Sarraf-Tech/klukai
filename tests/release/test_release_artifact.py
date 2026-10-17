from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from tools.ci import release_artifact as ra

REV = "a" * 40
LAYERS = ["sha256:" + c * 64 for c in "abc"]


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
        "reproducibility.json": {"identical": True},
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
            return_value={"layers": LAYERS, "revision": REV, "user": "1000:1000"},
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
