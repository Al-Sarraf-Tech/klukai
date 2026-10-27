"""Pure portrait face detection, feathered masks and crop geometry."""

from __future__ import annotations

from colorsys import rgb_to_hsv
from dataclasses import dataclass
import io
import logging

from PIL import Image, ImageDraw, ImageFilter
from .portrait_style import FRAME_REGIONS, MASK_FEATHER, CROP_SIDE_D, CROP_CENTER_DY

logger = logging.getLogger(__name__)
_SCAN = 4


@dataclass(frozen=True)
class Face:
    mid_x: float  # midpoint between her eyes, source pixels
    eye_y: float
    d: float  # eye distance
    found: bool = True


def _green_blobs(img: Image.Image) -> list[tuple[float, float, int]]:
    """Centroids (source px) + sizes of saturated green blobs in the face zone."""
    small = img.convert("RGB").resize(
        (max(1, img.width // _SCAN), max(1, img.height // _SCAN))
    )
    w, h = small.size
    data = small.tobytes()
    hits: set[tuple[int, int]] = set()
    for y in range(int(h * 0.08), int(h * 0.6)):
        for x in range(int(w * 0.15), int(w * 0.85)):
            i = (y * w + x) * 3
            hue, sat, val = rgb_to_hsv(
                data[i] / 255, data[i + 1] / 255, data[i + 2] / 255
            )
            if 0.13 <= hue <= 0.45 and sat >= 0.45 and val >= 0.35:
                hits.add((x, y))
    blobs = []
    while hits:
        stack = [hits.pop()]
        pts = []
        while stack:
            cx, cy = stack.pop()
            pts.append((cx, cy))
            for n in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                if n in hits:
                    hits.remove(n)
                    stack.append(n)
        if len(pts) >= 4:
            blobs.append(
                (
                    sum(p[0] for p in pts) / len(pts) * _SCAN,
                    sum(p[1] for p in pts) / len(pts) * _SCAN,
                    len(pts),
                )
            )
    return blobs


def locate_face(png: bytes) -> Face:
    """Find her eyes (Klukai's canon green irises) in a portrait base.

    Picks the best-matched pair of green blobs: level, similar size, a
    plausible eye distance, close to centre. Falls back to where the fixed
    portrait composition puts the face.
    """
    with Image.open(io.BytesIO(png)) as img:
        W, H = img.size
        blobs = _green_blobs(img)
    best: tuple[float, Face] | None = None
    for i, a in enumerate(blobs):
        for b in blobs[i + 1 :]:
            (x1, y1, n1), (x2, y2, n2) = sorted((a, b))
            d = x2 - x1
            if (
                not 0.06 * W <= d <= 0.3 * W
                or abs(y1 - y2) > 0.25 * d
                or max(n1, n2) > 3 * min(n1, n2)
            ):
                continue
            score = n1 + n2 - abs((x1 + x2) / 2 - W / 2) / W * 200
            if best is None or score > best[0]:
                best = (score, Face((x1 + x2) / 2, (y1 + y2) / 2, d))
    if best:
        return best[1]
    logger.warning("Portrait eyes not found; using the composition's default face box")
    return Face(W * 0.5, H * 0.27, W * 0.16, found=False)


def frame_mask(size: tuple[int, int], face: Face, frame: str) -> bytes:
    """A feathered white-on-black PNG mask of ``frame``'s repaint region."""
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for cx, cy, rx, ry in FRAME_REGIONS[frame]:
        x, y = face.mid_x + cx * face.d, face.eye_y + cy * face.d
        draw.ellipse(
            (x - rx * face.d, y - ry * face.d, x + rx * face.d, y + ry * face.d),
            fill=255,
        )
    mask = mask.filter(ImageFilter.GaussianBlur(MASK_FEATHER * face.d))
    buf = io.BytesIO()
    mask.save(buf, "PNG")
    return buf.getvalue()


def face_box(face: Face, size: tuple[int, int]) -> tuple[int, int, int, int]:
    """The square head crop (clamped inside the image) the expressions inpaint."""
    w, h = size
    side = int(min(CROP_SIDE_D * face.d, w, h))
    x0 = int(max(0, min(w - side, face.mid_x - side / 2)))
    y0 = int(max(0, min(h - side, face.eye_y + CROP_CENTER_DY * face.d - side / 2)))
    return x0, y0, x0 + side, y0 + side


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _iris_pixels(img: Image.Image, mask: Image.Image) -> int:
    """Green-iris pixels under the white of ``mask`` (half resolution)."""
    box = mask.getbbox()
    if box is None:
        return 0
    small = (max(1, (box[2] - box[0]) // 2), max(1, (box[3] - box[1]) // 2))
    rgb = img.convert("RGB").crop(box).resize(small).tobytes()
    m = mask.crop(box).resize(small).tobytes()
    count = 0
    for i, weight in enumerate(m):
        if weight >= 128:
            hue, sat, val = rgb_to_hsv(
                rgb[3 * i] / 255, rgb[3 * i + 1] / 255, rgb[3 * i + 2] / 255
            )
            if 0.13 <= hue <= 0.45 and sat >= 0.45 and val >= 0.35:
                count += 1
    return count
