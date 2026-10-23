"""klukai-agy-images.service: Seedream 4.5 on fal is the sole provider, capped."""

from pathlib import Path

UNIT = Path(__file__).resolve().parents[1] / "systemd/klukai-agy-images.service"


def lines():
    return UNIT.read_text().splitlines()


def test_fal_is_the_only_renderer_and_claude_still_checks():
    text = UNIT.read_text()
    assert "Environment=KLUKAI_ACCOUNT_IMAGE_PROVIDERS=fal" in lines()
    assert "Environment=KLUKAI_IMAGE_CHECKER=claude" in lines()
    for name in ("grok", "codex", "agy"):
        assert f"PROVIDERS={name}" not in text and f",{name}" not in text


def test_paid_calls_have_daily_and_monthly_caps():
    assert "Environment=KLUKAI_FAL_DAILY_LIMIT=10" in lines()
    assert "Environment=KLUKAI_FAL_MONTHLY_LIMIT=150" in lines()
