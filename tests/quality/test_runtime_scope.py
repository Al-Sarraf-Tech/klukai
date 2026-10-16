"""Structure and complexity cover the maintained runtime beyond the core."""

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/quality"))
from checks import complexity, structure  # noqa: E402
from evidence import InvalidEvidence  # noqa: E402
from runtime_scope import runtime_sources  # noqa: E402


@pytest.mark.parametrize(
    "name",
    [
        "docker/voice/app/main.py",
        "docker/events-bridge/main.py",
        "tgbot/bot.py",
        "ops/dominus-nobara/lmstudio-compat/gateway/main.py",
        "flutter_app/web/companion/avatar/renderer.js",
        "flutter_app/web/companion/vendor/project-runtime.js",
    ],
)
def test_oversized_served_component_is_rejected(tmp_path, name):
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    path.write_text("# oversized\n" * 500)
    with pytest.raises(InvalidEvidence, match="oversized"):
        structure(tmp_path, 500)


def test_complexity_measures_every_maintained_python_runtime(monkeypatch, tmp_path):
    names = [
        "docker/core/app/domain.py",
        "docker/voice/app/main.py",
        "docker/events-bridge/main.py",
        "tgbot/bot.py",
        "ops/dominus-nobara/lmstudio-compat/gateway/main.py",
    ]
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True)
        path.write_text("def answer():\n    return 42\n")
    calls = []

    def radon(argv, cwd):
        calls.append(argv)
        return json.dumps(
            {name: [{"type": "function", "complexity": 1}] for name in names}
        )

    monkeypatch.setattr("checks.run", radon)
    assert "5 callables" in complexity(tmp_path, 10, 20)
    assert set(calls[0][2:-1]) == set(names)


def test_only_attributed_generated_bundle_is_outside_handwritten_inventory(tmp_path):
    generated = "flutter_app/web/companion/vendor/three-avatar.min.js"
    maintained = {
        "flutter_app/web/companion/vendor/project-runtime.js",
        "flutter_app/web/companion/avatar/three-avatar.min.js",
    }
    for name in {generated, *maintained}:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("/* runtime */\n" * 500)
    measured = {
        path.relative_to(tmp_path).as_posix() for path in runtime_sources(tmp_path)
    }
    assert measured == maintained
