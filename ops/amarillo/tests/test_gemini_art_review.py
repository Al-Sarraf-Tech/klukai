"""Review manifests must not quietly upload changed files or enable paid auth."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

from PIL import Image
import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts/gemini_art_review.py"


def load_module():
    assert SCRIPT.is_file(), "Gemini artwork reviewer is not implemented"
    spec = importlib.util.spec_from_file_location("gemini_art_review", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def manifest(tmp_path):
    image = tmp_path / "outfit.png"
    Image.new("RGB", (64, 64), "green").save(image)
    entry = {"path": str(image), "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}
    data = {"version": 1, "references": [entry], "images": [
        {**entry, "id": "winter_patrol", "expected": ["white scarf", "black coat"]}
    ]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data))
    return path


def test_manifest_rejects_changed_image_before_any_cloud_request(manifest):
    review = load_module()
    Image.new("RGB", (64, 64), "red").save(manifest.parent / "outfit.png")
    with pytest.raises(review.ReviewError, match="digest"):
        review.load_manifest(manifest)


def test_manifest_rejects_duplicate_image_ids(manifest):
    review = load_module()
    data = json.loads(manifest.read_text())
    data["images"].append(data["images"][0])
    manifest.write_text(json.dumps(data))
    with pytest.raises(review.ReviewError, match="unique"):
        review.load_manifest(manifest)


def test_default_command_previews_without_credentials_or_cli(manifest, tmp_path):
    load_module()
    result = subprocess.run([sys.executable, str(SCRIPT), "--manifest", str(manifest)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    result = json.loads(result.stdout)
    assert result["mode"] == "preview"
    assert result["image_ids"] == ["winter_patrol"]


def test_result_parser_rejects_missing_or_invented_image_verdict():
    review = load_module()
    with pytest.raises(review.ReviewError, match="result"):
        review.parse_response(json.dumps({"status":"SUCCESS", "structured_output":{"id":"other","checks":[]}}), "winter_patrol")


def test_result_parser_keeps_visible_evidence_and_unknowns():
    review = load_module()
    result = {"id": "winter_patrol", "summary": "Scarf is visible; hands are occluded.",
              "checks": [{"criterion": "white scarf", "status": "match", "evidence": "White fabric at neck"},
                         {"criterion": "hands", "status": "not_visible", "evidence": "Outside frame"}]}
    outer = {'status':'SUCCESS', 'structured_output':result, 'model':'test-model'}
    parsed = review.parse_response(json.dumps(outer), "winter_patrol")
    assert parsed["review"]["checks"][1]["status"] == "not_visible"
    assert parsed["models"] == ["test-model"]


def test_result_parser_does_not_accept_cli_error_as_review():
    review = load_module()
    with pytest.raises(review.ReviewError, match="CLI"):
        review.parse_response('{"error":{"message":"credential secret must not be echoed"}}', "winter_patrol")
