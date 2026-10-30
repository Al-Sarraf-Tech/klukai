"""The retired xAI image API slugs must not return anywhere in the tree."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FORBIDDEN = ("grok-imagine-image-quality", "grok-imagine-image-pro")
SKIP = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache"}


def test_retired_image_api_slugs_are_absent():
    hits = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP for part in path.parts):
            continue
        if path.suffix.lower() not in {".py", ".md", ".yml", ".yaml", ".toml", ".service", ".json", ".txt"}:
            continue
        if path.name == Path(__file__).name:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            continue
        for slug in FORBIDDEN:
            if slug in text:
                hits.append(f"{path.relative_to(ROOT)}:{slug}")
    assert hits == []
