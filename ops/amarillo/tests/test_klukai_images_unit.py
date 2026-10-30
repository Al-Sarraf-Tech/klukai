"""klukai-agy-images.service: account-login Grok is the provider, capped."""

from pathlib import Path

UNIT = Path(__file__).resolve().parents[1] / "systemd/klukai-agy-images.service"


def lines():
    return UNIT.read_text().splitlines()


def test_grok_is_the_only_renderer_and_grok_checks():
    text = UNIT.read_text()
    assert "Environment=KLUKAI_ACCOUNT_IMAGE_PROVIDERS=grok" in lines()
    assert "Environment=KLUKAI_IMAGE_CHECKER=grok" in lines()
    assert "Environment=GROK_AUTH_PATH=%h/.grok/auth.json" in lines()
    for name in ("fal", "codex", "agy"):
        assert f"PROVIDERS={name}" not in text and f",{name}" not in text


def test_renders_have_daily_and_monthly_caps_and_no_fal_selection():
    text = UNIT.read_text()
    assert "Environment=KLUKAI_GROK_DAILY_LIMIT=10" in lines()
    assert "Environment=KLUKAI_GROK_MONTHLY_LIMIT=150" in lines()
    assert "KLUKAI_FAL_DAILY_LIMIT" not in text
    assert "KLUKAI_FAL_MONTHLY_LIMIT" not in text


def test_shutdown_allows_an_inflight_picture_to_finish_before_group_kill():
    settings = dict(line.split("=", 1) for line in lines() if "=" in line and not line.startswith("#"))
    # The main process stops admission and joins request threads. Do not send
    # SIGTERM to its reviewer/provider children before that drain can finish.
    assert settings["KillMode"] == "mixed"
    assert int(settings["TimeoutStopSec"]) >= 420  # worker360s plus transport/shutdown margin
