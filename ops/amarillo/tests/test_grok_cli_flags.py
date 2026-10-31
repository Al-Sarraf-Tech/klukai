"""Flags passed to pinned Grok CLI 1.0.62, checked against its --help when present."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import grok_account_chat as chat  # noqa: E402
import grok_image_check as check  # noqa: E402
import grok_image_render as render  # noqa: E402

# The argv flags these three scripts pass. 1.0.62 rejects --system-prompt-file.
CHAT_FLAGS = [
    "--session-id",
    "--model",
    "--effort",
    "--no-subagents",
    "--disable-web-search",
    "--permission-mode",
    "--max-turns",
    "--output-format",
    "--include-partial-messages",
    "--verbatim",
    "--tools",
    "--disallowed-tools",
    "--system-prompt-override",
    "--prompt-json",
]
CHECK_FLAGS = [
    "--session-id",
    "--model",
    "--effort",
    "--no-subagents",
    "--disable-web-search",
    "--permission-mode",
    "--max-turns",
    "--output-format",
    "--verbatim",
    "--system-prompt-override",
    "--tools",
    "--disallowed-tools",
    "--prompt-file",
]
RENDER_FLAGS = [
    "--session-id",
    "--model",
    "--effort",
    "--tools",
    "--no-subagents",
    "--disable-web-search",
    "--permission-mode",
    "--allow",
    "--max-turns",
    "--output-format",
    "--single",
]
ALIASES = {"--effort": "--reasoning-effort"}


def _flags(args: list[str]) -> list[str]:
    return [arg for arg in args if arg.startswith("--")]


def _version(binary: Path) -> str | None:
    try:
        completed = subprocess.run(
            [str(binary), "--version"], check=False, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.match(r"grok (\d+\.\d+\.\d+)\b", completed.stdout.strip())
    return found.group(1) if found else None


def _pinned_binary() -> Path | None:
    """The pinned build only. A ``grok`` on PATH counts only if it reports the
    pinned version: a newer CLI accepting a flag says nothing about 1.0.62."""
    name = f"grok-{render.VERSION}-linux-x86_64"
    candidates = [
        render.PINNED_DIR / name,
        Path.home() / ".grok" / "downloads" / name,
    ]
    found = shutil.which("grok")
    if found:
        candidates.append(Path(found))
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK) and _version(path) == render.VERSION:
            return path
    return None


def _assert_help_lists_the_contract_flags(binary: Path) -> None:
    completed = subprocess.run(
        [str(binary), "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    help_text = completed.stdout + completed.stderr
    assert completed.returncode == 0, help_text
    for flag in dict.fromkeys([*CHAT_FLAGS, *CHECK_FLAGS, *RENDER_FLAGS]):
        names = (flag, ALIASES[flag]) if flag in ALIASES else (flag,)
        assert any(re.search(re.escape(name) + r"\b", help_text) for name in names), flag
    assert "streaming-messages-json" in help_text
    assert "--include-partial-messages" in help_text


def test_chat_check_and_render_pass_only_the_contract_flags():
    chat_args = chat.arguments("s", "persona", "hello", "low")
    check_args = check.arguments("s", Path("prompt.json"))
    render_args = render.image_arguments("s", "draw")
    assert _flags(chat_args) == CHAT_FLAGS
    assert _flags(check_args) == CHECK_FLAGS
    assert _flags(render_args) == RENDER_FLAGS
    for args in (chat_args, check_args, render_args):
        assert args[args.index("--output-format") + 1] == "streaming-messages-json"
        assert "--system-prompt-file" not in args
    assert "--include-partial-messages" in chat_args
    assert "--prompt-json" in chat_args
    for name in ("grok_account_chat.py", "grok_image_check.py", "grok_image_render.py"):
        source = Path(render.__file__).with_name(name).read_text()
        assert '"--system-prompt-file"' not in source
        assert "'--system-prompt-file'" not in source


def test_pinned_cli_help_lists_every_flag_the_scripts_pass():
    binary = _pinned_binary()
    if binary is None:
        pytest.skip(f"pinned Grok CLI {render.VERSION} is absent")
    _assert_help_lists_the_contract_flags(binary)


def test_an_upgrade_candidate_accepts_every_flag_the_scripts_pass():
    """Set KLUKAI_GROK_CLI_CANDIDATE to a newer binary before moving the pin."""
    candidate = os.environ.get("KLUKAI_GROK_CLI_CANDIDATE")
    if not candidate:
        pytest.skip("KLUKAI_GROK_CLI_CANDIDATE is not set")
    binary = Path(candidate)
    assert binary.is_file() and os.access(binary, os.X_OK), candidate
    assert _version(binary) is not None, "candidate does not report a grok version"
    _assert_help_lists_the_contract_flags(binary)
