"""The PWA release tree is a pure function of the CI-built bundle."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.ci import web_release  # noqa: E402
from tools.ci.frontend_evidence import InvalidEvidence, inventory  # noqa: E402

BOOTSTRAP = (
    'var _flutter={};_flutter.buildConfig={"engineRevision":"x",'
    '"builds":[{"compileTarget":"dart2js","renderer":"canvaskit",'
    '"mainJsPath":"main.dart.js"}],"useLocalCanvasKit":true};'
    'function c(u){return u};var f=c("main.dart.js");\n'
)
INDEX = '<!DOCTYPE html><html><head><base href="/app/"></head><body>' \
    '<script src="flutter_bootstrap.js" async></script></body></html>\n'

# deploy-web.sh's original Step 4, kept verbatim as the reference behaviour.
SHELL_STEP_4 = r"""
cd "$1"
rm -f main.dart.*.js flutter_bootstrap.*.js
MAIN_HASH=$(md5sum main.dart.js | cut -c1-8)
MAIN_HASHED="main.dart.${MAIN_HASH}.js"
mv main.dart.js "$MAIN_HASHED"
sed -i "s|\"mainJsPath\":\"main.dart.js\"|\"mainJsPath\":\"$MAIN_HASHED\"|g" flutter_bootstrap.js
sed -i "s|c(\"main.dart.js\")|c(\"$MAIN_HASHED\")|g" flutter_bootstrap.js
grep -q "$MAIN_HASHED" flutter_bootstrap.js
BOOT_HASH=$(md5sum flutter_bootstrap.js | cut -c1-8)
BOOT_HASHED="flutter_bootstrap.${BOOT_HASH}.js"
mv flutter_bootstrap.js "$BOOT_HASHED"
sed -i "s|flutter_bootstrap.js|$BOOT_HASHED|g" index.html
grep -q "$BOOT_HASHED" index.html
"""


def build(path: Path, main: bytes = b"void main(){}\n") -> Path:
    path.mkdir()
    (path / "main.dart.js").write_bytes(main)
    (path / "flutter_bootstrap.js").write_text(BOOTSTRAP)
    (path / "index.html").write_text(INDEX)
    (path / "login.html").write_text("<html>login</html>\n")
    (path / "canvaskit").mkdir()
    (path / "canvaskit" / "canvaskit.wasm").write_bytes(b"\0asm")
    return path


def test_matches_the_shell_step_it_replaces(tmp_path):
    python_tree = build(tmp_path / "python")
    shell_tree = build(tmp_path / "shell")
    names = web_release.content_hash(python_tree)
    subprocess.run(["bash", "-euo", "pipefail", "-c", SHELL_STEP_4, "_", str(shell_tree)], check=True)
    assert inventory(python_tree) == inventory(shell_tree)
    main_md5 = hashlib.md5(b"void main(){}\n").hexdigest()[:8]
    assert names["main"] == f"main.dart.{main_md5}.js"
    assert (python_tree / names["bootstrap"]).is_file()
    assert names["bootstrap"] in (python_tree / "index.html").read_text()
    assert not (python_tree / "main.dart.js").exists()
    assert not (python_tree / "flutter_bootstrap.js").exists()


def test_is_deterministic(tmp_path):
    first = build(tmp_path / "a")
    second = build(tmp_path / "b")
    assert web_release.content_hash(first) == web_release.content_hash(second)
    assert inventory(first) == inventory(second)


def test_a_changed_bundle_gets_new_names(tmp_path):
    old = web_release.content_hash(build(tmp_path / "old"))
    new = web_release.content_hash(build(tmp_path / "new", b"void main(){print(1);}\n"))
    assert old["main"] != new["main"] and old["bootstrap"] != new["bootstrap"]


@pytest.mark.parametrize(
    "fault",
    ["no-main", "no-bootstrap", "bootstrap-without-reference", "index-without-reference", "already-hashed"],
)
def test_refuses_a_bundle_it_cannot_rewrite_completely(tmp_path, fault):
    tree = build(tmp_path / "t")
    if fault == "no-main":
        (tree / "main.dart.js").unlink()
    elif fault == "no-bootstrap":
        (tree / "flutter_bootstrap.js").unlink()
    elif fault == "bootstrap-without-reference":
        (tree / "flutter_bootstrap.js").write_text("var nothing;\n")
    elif fault == "index-without-reference":
        (tree / "index.html").write_text("<html></html>\n")
    else:
        (tree / "main.dart.0123abcd.js").write_text("stale\n")
    with pytest.raises(InvalidEvidence):
        web_release.content_hash(tree)


def test_cli_prints_names_and_tree_digest(tmp_path):
    tree = build(tmp_path / "cli")
    out = subprocess.run(
        [sys.executable, "-m", "tools.ci.web_release", "hash", str(tree)],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    doc = json.loads(out.stdout)
    assert doc["sha256"] == inventory(tree)["sha256"]
    assert (tree / doc["main"]).is_file() and (tree / doc["bootstrap"]).is_file()


def test_deploy_web_script_uses_the_shared_transform():
    script = (ROOT / "scripts/deploy-web.sh").read_text()
    assert "tools.ci.web_release hash" in script
    assert "md5sum main.dart.js" not in script


def test_copy_then_hash_leaves_the_source_bundle_untouched(tmp_path):
    source = build(tmp_path / "ci")
    before = inventory(source)
    release = tmp_path / "release"
    shutil.copytree(source, release)
    web_release.content_hash(release)
    assert inventory(source) == before
