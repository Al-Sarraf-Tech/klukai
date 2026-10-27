"""Klukai must remain on Amarillo even if the old heartbeat is fresh."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.ci import pool  # noqa: E402

NOW = 1_800_000_000


@pytest.mark.parametrize("beat,expected", [
    (str(NOW - 30), "amarillo"),
    (str(NOW - 150), "amarillo"),
    (str(NOW - 150 - 1), "amarillo"),   # stale: Dominus off or stuck
    (str(NOW + 30), "amarillo"),                        # small clock skew
    (str(NOW + 60 + 1), "amarillo"),    # a future beat is not trusted
    ("0", "amarillo"),                                 # Dominus said it cannot take runs
    ("", "amarillo"),                                  # the variable does not exist yet
    ("garbage", "amarillo"),
    ("-5", "amarillo"),
])
def test_no_heartbeat_can_restore_the_retired_pool(beat, expected):
    assert pool.plan(beat, NOW)["pool"] == expected


@pytest.mark.parametrize("beat", [str(NOW - 10), "0"])
def test_only_known_label_sets_come_out(beat):
    out = pool.plan(beat, NOW)
    for group in ("build", "serial", "frontend"):
        labels = json.loads(out[f"labels_{group}"])
        assert labels == ["self-hosted", "Linux", "X64", f"klukai-{out['pool']}-{group}"]


def test_the_cli_writes_github_output_lines():
    result = subprocess.run([sys.executable, "-m", "tools.ci.pool", "--heartbeat", str(NOW - 5),
                             "--now", str(NOW)], cwd=ROOT, capture_output=True, text=True, check=True)
    lines = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert lines["pool"] == "amarillo" and json.loads(lines["labels_build"])[-1] == "klukai-amarillo-build"
