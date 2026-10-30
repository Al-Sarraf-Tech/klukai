"""The smoke summary names model and effort and does not echo secrets."""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "klukai-grok-smoke.py"
spec = importlib.util.spec_from_file_location("klukai_grok_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def test_summary_lines_are_pass_fail_without_secret_text():
    assert smoke._line("chat", True, "model=grok-4.7 effort=medium source=stream") == (
        "chat: pass model=grok-4.7 effort=medium source=stream"
    )
    assert smoke._line("image", False, "ProviderUnavailable") == "image: fail ProviderUnavailable"
    printed = Path(smoke.__file__).read_text().split("print(", 1)[1]
    assert "Authorization" not in printed and "XAI_API_KEY" not in printed
