"""Private 3D appearances, separate from the canonical outfit/unlock catalog.

Only reviewed, fixed filenames are addressable. Model binaries never enter Git.
The intended-use notice does not replace any creator's original license.
"""

from pathlib import Path

NOTICE = "For personal, noncommercial use only."
MODELS = (
    {
        "id": "default",
        "name": "Klukai · Blazing Star",
        "creator": "MICA Team / Sunborn",
        "license": "Personal-use baseline",
        "source": "",
        "description": "Your pinned, approved appearance.",
    },
    {
        "id": "hk416_vroid",
        "name": "HK416 · VRoid",
        "creator": "こな麦茶",
        "license": "Creator terms: personal nonprofit avatar use; no redistribution",
        "source": "https://hub.vroid.com/en/characters/7421423711420001864/models/4935140573537488161",
        "description": "Fan rendition. Creator also prohibits sexual and violent uses. Download requires Pixiv sign-in.",
    },
    *(
        {
            "id": model_id,
            "name": f"Klukai · {outfit} (Garf)",
            "creator": "Garf",
            "license": "CC BY-NC-ND 4.0 (uploader-selected)",
            "source": "https://open3dlab.com/project/9ed8198a-df22-46db-81dd-676d0d6da019/",
            "description": "Private noncommercial adaptation only; no sharing adapted files. Underlying game-asset rights are unverified.",
        }
        for model_id, outfit in (
            ("klukai_garf", "Blazing Star"),
            ("garf_dorm", "Dorm"),
            ("garf_speed_star", "Speed Star"),
            ("garf_astral_luminous", "Astral Luminous"),
            ("garf_cerulean_breaker", "Cerulean Breaker"),
        )
    ),
)
_IDS = frozenset(m["id"] for m in MODELS)


def model_path(model_id: str, default: Path) -> Path | None:
    if model_id not in _IDS:
        return None
    if model_id == "default":
        return default
    return default.parent / "variants" / f"{model_id}.glb"


def catalog(default: Path) -> dict:
    return {
        "notice": NOTICE,
        "models": [
            {**m, "available": model_path(m["id"], default).is_file()} for m in MODELS
        ],
    }
