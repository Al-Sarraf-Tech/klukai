from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("materialize.py")
SPEC = importlib.util.spec_from_file_location("materialize_hf_cache", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
materialize = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(materialize)


class MaterializeTests(unittest.TestCase):
    def test_shared_snapshot_builds_both_standard_cache_views(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "release"
            destination = "speech/shared/huggingface/Example/model"
            snapshot = release / destination
            snapshot.mkdir(parents=True)
            (snapshot / "model.bin").write_bytes(b"model-bytes\n")
            (snapshot / "README.md").write_text(
                "---\nlibrary_name: ctranslate2\npipeline_tag: automatic-speech-recognition\n---\n",
                encoding="utf-8",
            )
            lock = {
                "snapshots": [
                    {
                        "id": "shared-test",
                        "enabled": True,
                        "destination": destination,
                        "source": {"repo": "Example/model", "revision": "abc123"},
                        "files": [
                            {
                                "path": "model.bin",
                                "size_bytes": 12,
                                "sha256": "9aa6c074d457a309644b6af534fb32e12e7f229b5bb5526425af241d6f50b946",
                            },
                            {
                                "path": "README.md",
                                "size_bytes": 77,
                                "sha256": "a657ad8a3a0efb415929ca87e3783a880f6529e67ae548831c256cf0d62a9faa",
                            },
                        ],
                    }
                ]
            }
            lock_path = root / "models.lock.json"
            lock_path.write_text(json.dumps(lock), encoding="utf-8")

            materialize.LOCK_PATH = lock_path
            materialize.RELEASE_ROOT = release
            materialize.CACHE_ROOTS = {
                "speaches": root / "speaches",
                "transcription": root / "transcription",
            }

            self.assertEqual(materialize.main(), 0)
            self.assertEqual(materialize.main(), 0)
            for cache in materialize.CACHE_ROOTS.values():
                repo = cache / "hub" / "models--Example--model"
                model_link = repo / "snapshots" / "abc123" / "model.bin"
                self.assertTrue(model_link.is_symlink())
                self.assertEqual(model_link.read_bytes(), b"model-bytes\n")
                self.assertEqual((repo / "refs" / "main").read_text(), "abc123\n")


if __name__ == "__main__":
    unittest.main()
