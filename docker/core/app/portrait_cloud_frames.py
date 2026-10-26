"""Explicit-only cloud animation edits; no model or GPU on either desktop."""

from __future__ import annotations
import asyncio
import hashlib
import io
import base64
import uuid
from PIL import Image
from . import agy_image_provider as provider
from .portrait_assets import (
    _write_atomic,
    _to_webp,
    frame_dir,
    frame_file,
    existing_frames,
)
from .portrait_style import SOURCE_NAME, CROP_PX

NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "klukai:explicit-portrait-animation:v1")
PROMPTS = {
    "blink": "Change only her eyelids: both eyes fully closed in a natural blink. Keep her mouth unchanged.",
    "talk": "Change only her mouth: slightly open as she speaks, a small natural opening. Keep both eyes unchanged.",
}


async def draw_missing(user_id: str, outfit_id: str, level: int) -> bool:
    from . import portrait, portrait_account

    if not portrait_account.enabled_for(user_id):
        return False
    if not await portrait_account.draw_base(user_id, outfit_id, level):
        return False
    source = frame_dir(user_id, outfit_id) / SOURCE_NAME
    if not source.is_file():
        # Existing cached sets occasionally have only their WebP base.
        source = frame_file(user_id, outfit_id, "base")
    base = source.read_bytes()
    missing = [
        frame for frame in PROMPTS if frame not in existing_frames(user_id, outfit_id)
    ]
    if not missing:
        return True
    plan = portrait._SetPlan(user_id, outfit_id, level, missing)
    crop, _ = await plan(base)
    with Image.open(io.BytesIO(crop)) as source_image:
        for quality in (80, 65, 50, 35):
            buf = io.BytesIO()
            source_image.convert("RGB").resize(
                (512, 512), Image.Resampling.LANCZOS
            ).save(buf, "JPEG", quality=quality)
            raw = buf.getvalue()
            if len(raw) <= 42000:
                break
        else:
            return False
    digest = hashlib.sha256(raw).hexdigest()
    encoded = base64.b64encode(raw).decode("ascii")
    for frame in missing:
        identity = str(uuid.uuid5(NAMESPACE, f"{user_id}:{outfit_id}:{digest}:{frame}"))
        source_descriptor = {"sha256": digest, "frame": frame, "image": encoded}
        result = await provider.submit_agy_image(
            PROMPTS[frame],
            user_id=user_id,
            width=CROP_PX,
            height=CROP_PX,
            include_commander=False,
            request_id=identity,
            portrait_source=source_descriptor,
        )
        if result.get("status") in {"pending", "unknown"}:
            result = await provider.recover_agy_image(
                PROMPTS[frame],
                user_id=user_id,
                width=CROP_PX,
                height=CROP_PX,
                include_commander=False,
                request_id=identity,
                portrait_source=source_descriptor,
            )
        rendered = result.get("image")
        if result.get("status") != "completed" or not rendered:
            return False
        with Image.open(io.BytesIO(rendered)) as pixels:
            normalized = portrait._png(
                pixels.convert("RGB").resize(
                    (CROP_PX, CROP_PX), Image.Resampling.LANCZOS
                )
            )
        if not plan.accept(frame, normalized):
            return False  # never buy automatic re-rolls for a failed blink
        composite = plan.composite(frame, normalized)
        _write_atomic(
            frame_file(user_id, outfit_id, frame),
            await asyncio.to_thread(_to_webp, portrait._png(composite)),
        )
    return True
