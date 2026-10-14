"""Pinned, offline launcher; --verify-only never imports Torch or starts HTTP."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parent


def main() -> int:
    clean = {key: value for key, value in os.environ.items()
             if not key.startswith("LD_") and key not in ("PYTHONPATH", "PYTHONHOME")}
    clean.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    if clean != dict(os.environ):
        os.execve(sys.executable, [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]], clean)
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    from artifacts import verify_artifacts, verify_file, verify_packages
    source_lock = json.loads((ROOT / "source.lock.json").read_text())
    for record in source_lock["files"]:
        verify_file(ROOT / record["path"], record)
    lock = json.loads((ROOT / "artifacts.lock.json").read_text())
    if platform.python_version() != lock["supervisor_python"]:
        raise ValueError("supervisor Python version differs from verified release")
    verify_packages(lock["supervisor_packages"])
    if args.verify_only:
        model_dir = Path(os.environ.get("XTTS_MODEL_DIR", str(Path.home() / ".cache/klukai/xtts-v2/6c2b0d75eae4b7047358e3b6bd9325f857d43f77")))
        verify_artifacts(lock, model_dir, ROOT / lock["reference"]["path"])
        print("Local voice release source, supervisor packages, model and reference digests verified; no inference.")
        return 0
    if not os.environ.get("VOICE_API_TOKEN"):
        raise ValueError("VOICE_API_TOKEN is required")
    # Imports are resolved from the checked release directory. Exactly one
    # HTTP process is essential to the single-owner GPU admission contract.
    import uvicorn
    sys.path.insert(0, str(ROOT.parent))
    uvicorn.run("voice_xpu.server:app", host="100.111.198.19", port=8301,
                workers=1, access_log=False, log_level="info", timeout_graceful_shutdown=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
