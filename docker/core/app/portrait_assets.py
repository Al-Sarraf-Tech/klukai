"""Portrait file layout, atomic encoding and signed user-scoped asset URLs."""

from __future__ import annotations
import io
import os
from pathlib import Path
import time
import uuid
from PIL import Image
from . import signed_urls
from .portrait_style import STYLE_VERSION, FRAME_NAMES, _SAFE_ID, SIGN_BUCKET_S


def portraits_root() -> Path:
    return Path(os.environ.get("IMAGES_DIR", "/images")) / "portraits" / STYLE_VERSION


def frame_dir(user_id: str, outfit_id: str) -> Path:
    """The frame directory; rejects anything that is not a plain id."""
    for part in (user_id, outfit_id):
        if not _SAFE_ID.fullmatch(part or ""):
            raise ValueError("unsafe portrait id")
    return portraits_root() / user_id / outfit_id


def frame_file(user_id: str, outfit_id: str, frame: str) -> Path:
    if frame not in FRAME_NAMES:
        raise ValueError(f"unknown portrait frame {frame!r}")
    return frame_dir(user_id, outfit_id) / f"{frame}.webp"


def existing_frames(user_id: str, outfit_id: str) -> set[str]:
    return {f for f in FRAME_NAMES if frame_file(user_id, outfit_id, f).is_file()}


def _resource(user_id: str, outfit_id: str, frame: str) -> str:
    return f"portrait:{user_id}/{outfit_id}/{frame}"


def _sign_ttl(now: float) -> int:
    """TTL that lands expiry on a bucket edge, so URLs stay byte-identical
    (one browser cache entry) for a whole bucket."""
    bucket_end = (int(now) // SIGN_BUCKET_S + 2) * SIGN_BUCKET_S
    return bucket_end - int(now)


def frame_url(user_id: str, outfit_id: str, frame: str, version: int) -> str:
    token = signed_urls.sign(
        _resource(user_id, outfit_id, frame),
        ttl_seconds=_sign_ttl(time.time()),
        user_id=user_id,
    )
    return f"/api/portrait/frame/{user_id}/{outfit_id}/{frame}.webp?v={version}&sig={token}"


def verify_frame(user_id: str, outfit_id: str, frame: str, sig: str) -> bool:
    return signed_urls.verify(
        sig, _resource(user_id, outfit_id, frame), user_id=user_id
    )


def _to_webp(png: bytes) -> bytes:
    buf = io.BytesIO()
    with Image.open(io.BytesIO(png)) as img:
        img.convert("RGB").save(buf, "WEBP", quality=92, method=4)
    return buf.getvalue()


def _write_atomic(path: Path, data: bytes) -> None:
    """Readers see the old file or the new one, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
