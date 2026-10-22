#!/usr/bin/env python3
"""Deterministic PWA release tree from a verified Flutter web build.

`hash <dir>` applies, in place, the cache-busting renames deploy-web.sh has
always made: main.dart.js becomes main.dart.<md5[:8]>.js (and the bootstrap's
two references follow), then flutter_bootstrap.js becomes
flutter_bootstrap.<md5[:8]>.js (and index.html follows). The same bundle always
yields the same tree, so amarillo can turn the CI-built bundle into the exact
release it signs and serves. Anything it cannot rewrite completely is refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

from tools.ci.frontend_evidence import InvalidEvidence, inventory, require

MAIN = "main.dart.js"
BOOTSTRAP = "flutter_bootstrap.js"
HASHED = re.compile(r"(main\.dart|flutter_bootstrap)\.[0-9a-f]{8}\.js\Z")


def _md5_8(path: Path) -> str:
    return hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()[:8]


def content_hash(tree: Path) -> dict[str, str]:
    """Rename the two entry bundles by content and rewrite their references."""
    require(tree.is_dir() and not tree.is_symlink(), f"Missing/linked tree: {tree}")
    main, bootstrap, index = tree / MAIN, tree / BOOTSTRAP, tree / "index.html"
    for path in (main, bootstrap, index):
        require(path.is_file() and not path.is_symlink(), f"Bundle lacks {path.name}")
    stale = sorted(p.name for p in tree.iterdir() if HASHED.fullmatch(p.name))
    require(not stale, f"Bundle already carries hashed entry files: {stale}")

    main_name = f"main.dart.{_md5_8(main)}.js"
    text = bootstrap.read_text(encoding="utf-8")
    text = text.replace(f'"mainJsPath":"{MAIN}"', f'"mainJsPath":"{main_name}"')
    text = text.replace(f'c("{MAIN}")', f'c("{main_name}")')
    require(main_name in text, "Bootstrap no longer references the main bundle")
    main.rename(tree / main_name)
    bootstrap.write_text(text, encoding="utf-8")

    boot_name = f"flutter_bootstrap.{_md5_8(bootstrap)}.js"
    page = index.read_text(encoding="utf-8").replace(BOOTSTRAP, boot_name)
    require(boot_name in page, "index.html no longer references the bootstrap")
    bootstrap.rename(tree / boot_name)
    index.write_text(page, encoding="utf-8")
    return {"main": main_name, "bootstrap": boot_name}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    h = sub.add_parser("hash")
    h.add_argument("tree", type=Path)
    args = parser.parse_args()
    try:
        names = content_hash(args.tree)
        print(json.dumps({**names, "sha256": inventory(args.tree)["sha256"]}, sort_keys=True))
    except (InvalidEvidence, OSError, UnicodeError) as error:
        print(f"web release: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
