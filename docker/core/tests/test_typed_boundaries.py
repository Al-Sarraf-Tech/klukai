"""Invalid provider/configuration values cannot masquerade as domain mappings."""

import pytest

from unittest.mock import AsyncMock, MagicMock

from app.llm_json import call_llm
from app.personality.loader import reload_personality


@pytest.mark.parametrize("raw", ["[]", "[1, 2]", "42", "true", "null", '"text"'])
@pytest.mark.asyncio
async def test_structured_llm_boundary_rejects_non_object_payloads(raw):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"choices": [{"message": {"content": raw}}]}
    client = MagicMock(post=AsyncMock(return_value=response))
    assert await call_llm("http://offline", "test", "prompt", client=client) == {}


@pytest.mark.parametrize("raw", ["[]", "42", "null", '"text"'])
def test_personality_requires_mapping(tmp_path, raw):
    path = tmp_path / "personality.yaml"
    path.write_text(raw)
    with pytest.raises(ValueError, match="mapping"):
        reload_personality(str(path))
