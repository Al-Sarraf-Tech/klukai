"""Invalid provider/configuration values cannot masquerade as domain mappings."""

import pytest

from unittest.mock import AsyncMock

from app import background_llm, extraction_llm
from app.personality.loader import reload_personality


@pytest.mark.parametrize("raw", ["[]", "[1, 2]", "42", "true", "null", '"text"'])
@pytest.mark.asyncio
async def test_structured_llm_boundary_rejects_non_object_payloads(raw, monkeypatch):
    monkeypatch.setattr(extraction_llm, "complete", AsyncMock(return_value=raw))
    assert await background_llm.json_object("typed", "prompt", max_tokens=64) is None


@pytest.mark.parametrize("raw", ["[]", "42", "null", '"text"'])
def test_personality_requires_mapping(tmp_path, raw):
    path = tmp_path / "personality.yaml"
    path.write_text(raw)
    with pytest.raises(ValueError, match="mapping"):
        reload_personality(str(path))
