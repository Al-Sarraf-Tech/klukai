"""Bounded inline Klukai face crops for explicit cloud edits; never file paths."""
import base64
import hashlib
import io
import re
from PIL import Image

MAX_INPUT_BYTES = 42000


def decode_source(source):
    if (type(source) is not dict or set(source) != {"sha256", "frame", "image"}
            or source.get("frame") not in {"blink", "talk"}
            or not isinstance(source.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", source["sha256"])
            or not isinstance(source.get("image"), str)
            or len(source["image"]) > 56000):
        raise ValueError("invalid portrait source")
    try:
        raw = base64.b64decode(source["image"], validate=True)
        if len(raw) > MAX_INPUT_BYTES or hashlib.sha256(raw).hexdigest() != source["sha256"]:
            raise ValueError("portrait source changed")
        with Image.open(io.BytesIO(raw)) as image:
            if image.size != (512, 512) or image.format != "JPEG":
                raise ValueError("invalid portrait crop")
            image.load()
    except (OSError, TypeError) as error:
        raise ValueError("invalid portrait crop") from error
    return raw
