"""Transport caches are optional; changed or linked bytes cannot be loaded."""

import pytest
from tools.ci import docker_cache


def test_record_binds_immutable_image_and_tar_bytes(tmp_path):
    archive = tmp_path / "image.tar"
    archive.write_bytes(b"image bytes")
    image = "sha256:" + "a" * 64
    record = tmp_path / "cache.json"
    docker_cache.publish(record, archive, image)
    assert docker_cache.read(record) == (archive, image)
    archive.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="digest"):
        docker_cache.read(record)


def test_invalid_image_and_symlink_are_rejected(tmp_path):
    archive = tmp_path / "image.tar"
    archive.write_bytes(b"image bytes")
    with pytest.raises(ValueError):
        docker_cache.publish(tmp_path / "cache.json", archive, "mutable:latest")
    linked = tmp_path / "linked.tar"
    linked.symlink_to(archive)
    with pytest.raises(ValueError):
        docker_cache.publish(tmp_path / "cache.json", linked, "sha256:" + "a" * 64)


def test_cache_restore_is_opportunistic_and_never_selects_corrupt_bytes(
    tmp_path, monkeypatch
):
    record = tmp_path / "cache.json"
    record.write_text("{}")
    calls = []
    monkeypatch.setattr(docker_cache.subprocess, "run", lambda *a, **k: calls.append(a))
    assert docker_cache.restore(record) is None
    assert calls == []


def test_buildkit_cache_validates_all_oci_blobs_before_reuse(tmp_path):
    directory = tmp_path / "buildkit"
    blobs = directory / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    content = b"layer"
    import hashlib

    layer = blobs / hashlib.sha256(content).hexdigest()
    layer.write_bytes(content)
    (directory / "index.json").write_text("{}")
    record = tmp_path / "buildkit.json"
    docker_cache.publish_buildkit(record, directory)
    assert docker_cache.read_buildkit(record) == directory
    layer.write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        docker_cache.read_buildkit(record)
