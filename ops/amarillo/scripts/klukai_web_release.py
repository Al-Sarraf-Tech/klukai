#!/usr/bin/env python3
"""Atomic PWA releases for Klukai, served by companion-core from one web root.

The web root is bind-mounted read-only into companion-core at /app/static:

    .releases/<id>/              one complete, verified release tree each
    .current -> .releases/<id>   the live release (relative symlink)
    <name> -> .current/<name>    one relative link per top-level entry

Core's StaticFiles (and the `/` login route) resolve every request through
`<name>` and `.current`, so renaming a fresh `.current` link over the old one
switches all files in one atomic step: a client never sees a mix of two
releases, and nothing is ever rewritten in place. Old releases stay until they
are pruned, so a rollback is the same one-rename switch back. A bind mount
follows the directory inode, which is why the indirection lives inside the
mount rather than in a symlinked mount source.

    klukai_web_release.py adopt --from ~/git/klukai/web-build   # one time, no live change
    klukai_web_release.py activate     # owner-approved: move core's mount here (one core recreate)
    klukai_web_release.py deploy <revision>   # a CI release relayed and signed on amarillo
    klukai_web_release.py rollback [--to <id>]
    klukai_web_release.py status

`deploy` verifies the signed web provenance (relayed by
klukai-fetch-ci-artifacts.py `web`), stages the release under `.releases/`,
checks every byte, switches, then fetches the entry files through core and
compares them with the signed digests. Any failure switches back to the
previous release and checks that too. Each run writes a receipt.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import sys
import time
from typing import Any
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
if __name__ == "__main__":
    # `activate` recreates core through klukai-release/klukai_compose, which read
    # this at import: an installed copy must drive the production checkout.
    os.environ.setdefault("KLUKAI_PRODUCTION_CHECKOUT", str(Path.home() / "git/klukai"))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))
import klukai_deploy_lock  # noqa: E402
from tools.ci.frontend_evidence import (  # noqa: E402
    InvalidEvidence,
    digest,
    inventory,
    tree_digest,
)

ROOT = Path.home() / ".local/share/klukai/web"
STATE = Path.home() / ".local/state/klukai/release/web"
RELEASES = Path.home() / ".local/state/klukai/ci-releases"
OVERRIDE = Path.home() / ".local/state/klukai/release/web-root.json"
LEGACY = Path.home() / "git/klukai/web-build"
BASE = "http://127.0.0.1:8300"
WEB_KIND = "klukai-web"
RESERVED = {".current", ".releases"}
RELEASE_ID = re.compile(r"(?:[0-9a-f]{12}|adopted-\d{8}t\d{6})-[0-9a-f]{12}\Z")
HASHED = re.compile(r"(main\.dart|flutter_bootstrap)\.[0-9a-f]{8}\.js\Z")
# Entry points a browser loads first, when the release has them.
SMOKE_FILES = (
    "flutter.js",
    "manifest.json",
    "version.json",
    "favicon.png",
    "companion.html",
    "canvaskit/canvaskit.js",
    "canvaskit/canvaskit.wasm",
)
IMMUTABLE = "public, max-age=31536000, immutable"
SUCCESS = {"deploy": {"deployed", "unchanged"}, "rollback": {"rolled_back"},
           "adopt": {"adopted"}, "activate": {"activated"}}


class WebError(RuntimeError):
    """A release cannot be staged, switched or verified."""


def _release_module() -> Any:
    """klukai-release.py (hyphenated): signature check and the rollout engine."""
    name = "klukai_release"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "klukai-release.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def release_id(revision: str, tree_sha256: str) -> str:
    return f"{revision[:12]}-{tree_sha256[:12]}"


def current(root: Path) -> str | None:
    link = root / ".current"
    if not link.is_symlink():
        return None
    target = os.readlink(link)
    rid = target.removeprefix(".releases/")
    if target == rid or not RELEASE_ID.fullmatch(rid):
        raise WebError(f"{link} points outside .releases: {target}")
    return rid


def tree(root: Path, rid: str) -> Path:
    if not RELEASE_ID.fullmatch(rid):
        raise WebError(f"invalid release id {rid!r}")
    return root / ".releases" / rid


# ── provenance ────────────────────────────────────────────────────────────────


def verify_provenance(directory: Path, revision: str, signers: Path | None = None) -> dict[str, Any]:
    """The signed web provenance binds this revision and every release byte."""
    release = _release_module()
    path = directory / "web-provenance.json"
    try:
        release.verify_signature(path, signers or release.SIGNERS)
    except release.ReleaseError as error:
        raise WebError(str(error)) from error
    doc = json.loads(path.read_text())
    if doc.get("schema") != 1 or doc.get("kind") != WEB_KIND:
        raise WebError("provenance is not a Klukai web release")
    if doc.get("revision") != revision:
        raise WebError("signed web provenance is for a different revision")
    try:
        found = inventory(directory / "release")
    except InvalidEvidence as error:
        raise WebError(str(error)) from error
    if found != doc.get("release"):
        raise WebError("release tree differs from the signed web provenance")
    return doc


# ── staging and switching ─────────────────────────────────────────────────────


def _publishable(path: Path) -> None:
    """Readable by core's user through the read-only mount, writable by no one else."""
    for entry in [path, *path.rglob("*")]:
        if entry.is_symlink():
            raise WebError(f"symlink in release tree: {entry}")
        entry.chmod(0o755 if entry.is_dir() else 0o644)


def _names(release: Path) -> list[str]:
    names = sorted(p.name for p in release.iterdir())
    bad = [n for n in names if n in RESERVED or n.startswith((".current", ".link-", ".releases"))]
    if bad:
        raise WebError(f"release uses reserved top-level names: {bad}")
    return names


def entry_files(release: Path) -> dict[str, str]:
    """The live release's own content-hashed entry pair: the bootstrap its
    index.html loads and the main bundle that bootstrap loads."""
    found: dict[str, str] = {}
    page = release / "index.html"
    boot = re.search(r"flutter_bootstrap\.[0-9a-f]{8}\.js", page.read_text()) if page.is_file() else None
    if boot and (release / boot.group()).is_file():
        found[boot.group()] = digest(release / boot.group())
        main = re.search(r"main\.dart\.[0-9a-f]{8}\.js", (release / boot.group()).read_text())
        if main and (release / main.group()).is_file():
            found[main.group()] = digest(release / main.group())
    _check_cache_names(release, found)
    return found


def _check_cache_names(release: Path, names: Iterable[str]) -> None:
    """Check the existing cache identifiers, not cryptographic provenance."""
    for name in names:
        if HASHED.fullmatch(name):
            actual = hashlib.md5((release / name).read_bytes(), usedforsecurity=False).hexdigest()[:8]
            if actual != name.rsplit(".", 2)[1]:
                raise WebError(f"entry bytes do not match their cache filename: {name}")


def is_release(found: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Exactly the verified release, plus only carried hashed entry bundles."""
    files, want = found["files"], expected["files"]
    return all(files.get(k) == v for k, v in want.items()) and all(
        HASHED.fullmatch(k) for k in files.keys() - want.keys()
    )


def with_carried(expected: dict[str, Any], carried: dict[str, str]) -> dict[str, Any]:
    files = {**carried, **expected["files"]}
    return {"files": files, "sha256": tree_digest(files)}


def stage(
    root: Path,
    source: Path,
    rid: str,
    expected: dict[str, Any],
    carry_from: Path | None = None,
    carried: dict[str, str] | None = None,
) -> Path:
    """Copy a release under .releases/<rid> and prove every byte; idempotent.

    `carried` names the previous release's own hashed entry bundles (from
    `carry_from`): a browser that loaded the previous index.html just before
    the switch still finds the bundles it asks for next. Their names embed
    their content hash, so they can never shadow a file of this release."""
    carried = {k: v for k, v in (carried or {}).items() if k not in expected["files"]}
    if carried and carry_from is None:
        raise WebError("carried bundles need their source release")
    expected = with_carried(expected, carried)
    releases = root / ".releases"
    releases.mkdir(parents=True, exist_ok=True)
    # Readable through core's read-only mount whatever the caller's umask.
    for directory in (root, releases):
        directory.chmod(0o755)
    final = tree(root, rid)
    if final.is_dir():
        if is_release(inventory(final), expected):
            return final  # a retry: the same verified release is already staged
        if current(root) == rid:
            raise WebError(f"live release {rid} differs from its provenance")
        shutil.rmtree(final)
    partial = releases / f".{rid}.partial"
    if partial.exists():
        shutil.rmtree(partial)
    shutil.copytree(source, partial, symlinks=True)
    for name in carried:
        assert carry_from is not None
        shutil.copyfile(carry_from / name, partial / name)
    _publishable(partial)
    if inventory(partial) != expected:
        shutil.rmtree(partial)
        raise WebError("staged release differs from its expected inventory")
    os.rename(partial, final)
    _fsync_dir(releases)
    return final


def ensure_links(root: Path, names: list[str]) -> list[str]:
    """Give every top-level name a stable `.current/<name>` link (created once)."""
    created = []
    for name in names:
        link, want = root / name, f".current/{name}"
        if link.is_symlink():
            if os.readlink(link) != want:
                raise WebError(f"{link} links to {os.readlink(link)}, not {want}")
            continue
        if link.exists():
            raise WebError(f"{link} is a real file or directory in the web root")
        staged = root / f".link-{secrets.token_hex(6)}"
        os.symlink(want, staged)
        os.replace(staged, link)
        created.append(name)
    if created:
        _fsync_dir(root)
    return created


def switch(root: Path, rid: str) -> None:
    """Make <rid> live in one rename (atomic for every reader)."""
    if not tree(root, rid).is_dir():
        raise WebError(f"release {rid} is not staged")
    staged = root / f".current.tmp-{secrets.token_hex(6)}"
    os.symlink(f".releases/{rid}", staged)
    os.replace(staged, root / ".current")
    _fsync_dir(root)


# ── smoke checks through the real server ──────────────────────────────────────


def http_get(url: str, timeout: float = 15) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        body = error.read()
        error.close()
        return error.code, body, dict(error.headers or {})


def smoke(
    base: str,
    root: Path,
    rid: str,
    *,
    fetch: Callable[[str], tuple[int, bytes, dict[str, str]]] = http_get,
    attempts: int = 3,
    delay: float = 1.0,
) -> list[str]:
    """Fetch the entry files through core and compare them with the release."""
    release = tree(root, rid)
    files = inventory(release)["files"]
    _check_cache_names(release, files)
    checks: list[tuple[str, str]] = [("/app/", "index.html")]
    if "login.html" in files:
        checks.append(("/", "login.html"))
    checks += [(f"/app/{name}", name) for name in sorted(files) if HASHED.fullmatch(name)]
    checks += [(f"/app/{name}", name) for name in SMOKE_FILES if name in files]
    if not any(HASHED.fullmatch(n) for n in files):
        raise WebError("release has no content-hashed entry bundles")
    passed = []
    for path, name in checks:
        for attempt in range(1, attempts + 1):
            try:
                status, body, headers = fetch(base.rstrip("/") + path)
            except OSError:  # refused or reset: a failed attempt, retried
                status, body, headers = 0, b"", {}
            ok = status == 200 and hashlib.sha256(body).hexdigest() == files[name]
            if ok and HASHED.fullmatch(name):
                ok = {k.lower(): v for k, v in headers.items()}.get("cache-control") == IMMUTABLE
            if ok:
                passed.append(f"{path} = {name}")
                break
            if attempt == attempts:
                raise WebError(f"{path} did not serve {name} of {rid} (HTTP {status})")
            time.sleep(delay)
    return passed


# ── state and receipts ────────────────────────────────────────────────────────


def _load(state: Path) -> dict[str, Any]:
    path = state / "state.json"
    return json.loads(path.read_text()) if path.is_file() else {"releases": {}}


def _save(state: Path, doc: dict[str, Any]) -> None:
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged = state / "state.json.tmp"
    staged.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    os.replace(staged, state / "state.json")


def receipt(state: Path, doc: dict[str, Any]) -> Path:
    directory = state / "receipts"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{doc['command']}.json"
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return path


def _record(state: Path, rid: str, previous: str | None, meta: dict[str, Any]) -> None:
    doc = _load(state)
    doc["releases"].setdefault(rid, {}).update(meta)
    if previous and previous != rid:
        doc["previous"] = previous
    doc["current"] = rid
    _save(state, doc)


# ── operations ────────────────────────────────────────────────────────────────


def deploy(
    revision: str,
    *,
    root: Path = ROOT,
    releases: Path = RELEASES,
    state: Path = STATE,
    base: str = BASE,
    signers: Path | None = None,
    fetch: Callable[[str], tuple[int, bytes, dict[str, str]]] = http_get,
    keep: int = 5,
) -> dict[str, Any]:
    directory = releases / revision / "web"
    doc: dict[str, Any] = {"command": "deploy", "revision": revision,
                           "started_at": datetime.now(UTC).isoformat()}
    provenance = verify_provenance(directory, revision, signers)
    previous = current(root)
    if previous is None:
        raise WebError(f"{root} has no live release; run `adopt` first")
    rid = release_id(revision, provenance["release"]["sha256"])
    doc.update(release=rid, previous=previous, ci_run=provenance["ci_run"])
    live_meta = _load(state)["releases"].get(previous, {})
    live_tree = tree(root, previous)
    if live_meta.get("tree_sha256") == provenance["release"]["sha256"]:
        # Metadata records a prior deployment, not the current bytes or
        # serving configuration. A core-only release can change either.
        if not is_release(inventory(live_tree), provenance["release"]):
            raise WebError("live release does not match the verified provenance")
        doc["checks"] = smoke(base, root, previous, fetch=fetch)
        doc.update(status="unchanged", release=previous, finished_at=datetime.now(UTC).isoformat())
        return doc
    carried = {} if rid == previous else {
        k: v for k, v in entry_files(live_tree).items() if k not in provenance["release"]["files"]}
    staged = stage(root, directory / "release", rid, provenance["release"],
                   carry_from=live_tree, carried=carried)
    doc["carried"] = sorted(carried)
    if rid == previous:
        doc["checks"] = smoke(base, root, previous, fetch=fetch)
        doc.update(status="unchanged", finished_at=datetime.now(UTC).isoformat())
        return doc
    doc["links_created"] = ensure_links(root, _names(staged))
    # From the switch on, production may be changed: every escape (failed
    # check, exception, SIGTERM via SystemExit, Ctrl-C) switches back.
    try:
        switch(root, rid)
        doc["checks"] = smoke(base, root, rid, fetch=fetch)
        _record(state, rid, previous, {"revision": revision, "ci_run": provenance["ci_run"],
                                       "tree_sha256": provenance["release"]["sha256"],
                                       "deployed_at": datetime.now(UTC).isoformat()})
        ok, detail = True, "every entry file served the signed bytes"
    except BaseException as error:  # noqa: BLE001 - restore on every escape
        ok, detail = False, f"{type(error).__name__}: {error}"[:300]
    doc["detail"] = detail
    if ok:
        doc["status"] = "deployed"
        try:
            doc["pruned"] = prune(root, state, keep=keep)
        except (OSError, WebError) as error:  # housekeeping never masks a deploy
            doc["pruned"] = {"error": str(error)[:200]}
    else:
        doc.update(_restore(root, previous, base, fetch))
    doc["finished_at"] = datetime.now(UTC).isoformat()
    return doc


def _restore(root: Path, previous: str, base: str, fetch: Callable[..., Any]) -> dict[str, Any]:
    try:
        switch(root, previous)
        back = smoke(base, root, previous, fetch=fetch)
        return {"status": "rolled_back", "rollback_checks": back}
    except BaseException as error:  # noqa: BLE001 - report, never hide
        return {"status": "rollback_failed",
                "rollback_detail": f"{type(error).__name__}: {error}"[:300]}


def rollback(
    to: str | None = None,
    *,
    root: Path = ROOT,
    state: Path = STATE,
    base: str = BASE,
    fetch: Callable[[str], tuple[int, bytes, dict[str, str]]] = http_get,
    allow_live: bool = False,
) -> dict[str, Any]:
    """Switch back to the previous (or a named) staged release and verify it.

    With `allow_live` (the deployer's restore), a target that is already live
    is re-checked rather than refused: a failed switch back may have left the
    right release live but unverified."""
    saved = _load(state)
    live = current(root)
    target = to or saved.get("previous")
    if target and target == live and allow_live:
        doc: dict[str, Any] = {"command": "rollback", "from": live, "to": target,
                               "started_at": datetime.now(UTC).isoformat()}
        try:
            doc["checks"] = smoke(base, root, target, fetch=fetch)
            doc["status"] = "rolled_back"
        except (WebError, InvalidEvidence, OSError) as error:
            doc.update(status="rollback_failed", detail=str(error)[:300])
        doc["finished_at"] = datetime.now(UTC).isoformat()
        return doc
    if not target or target == live:
        raise WebError("no previous release to roll back to")
    if not tree(root, target).is_dir():
        raise WebError(f"release {target} is no longer staged")
    doc: dict[str, Any] = {"command": "rollback", "from": live, "to": target,
                           "started_at": datetime.now(UTC).isoformat()}
    try:
        switch(root, target)
        doc["checks"] = smoke(base, root, target, fetch=fetch)
        doc["status"] = "rolled_back"
        _record(state, target, live, {"rolled_back_at": datetime.now(UTC).isoformat()})
    except BaseException as error:  # noqa: BLE001 - put the live release back
        doc["detail"] = f"{type(error).__name__}: {error}"[:300]
        restored = _restore(root, live, base, fetch) if live else {"status": "rollback_failed"}
        # The target failed its checks; the live release is back (or reported).
        doc["status"] = "target_failed" if restored["status"] == "rolled_back" else "rollback_failed"
        doc["restore"] = restored
    doc["finished_at"] = datetime.now(UTC).isoformat()
    return doc


def adopt(source: Path, *, root: Path = ROOT, state: Path = STATE) -> dict[str, Any]:
    """Import the live web-build as the first release. Changes nothing that is served."""
    if current(root) is not None:
        raise WebError(f"{root} already has a live release")
    found = inventory(source)  # refuses symlinks and empty trees
    _check_cache_names(source, found["files"])
    rid = f"adopted-{datetime.now(UTC).strftime('%Y%m%dt%H%M%S')}-{found['sha256'][:12]}"
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    staged = stage(root, source, rid, found)
    ensure_links(root, _names(staged))
    switch(root, rid)
    _record(state, rid, None, {"revision": None, "adopted_from": str(source),
                               "tree_sha256": found["sha256"],
                               "deployed_at": datetime.now(UTC).isoformat()})
    return {"command": "adopt", "status": "adopted", "release": rid,
            "files": len(found["files"]), "tree_sha256": found["sha256"]}


def prune(root: Path, state: Path, keep: int = 5) -> dict[str, int]:
    """Keep the live and previous releases plus the newest `keep`; drop dead links."""
    saved = _load(state)
    protected = {current(root), saved.get("previous")} - {None}
    releases = root / ".releases"
    staged = sorted((p for p in releases.iterdir() if RELEASE_ID.fullmatch(p.name) and p.is_dir()),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    survivors = {p.name for p in staged[:keep]} | protected
    removed = 0
    for path in staged:
        if path.name not in survivors:
            shutil.rmtree(path)
            removed += 1
    for leftover in releases.glob(".*.partial"):
        shutil.rmtree(leftover)
    names = set()
    for rid in survivors:
        names.update(p.name for p in tree(root, rid).iterdir())
    links = 0
    for entry in root.iterdir():
        if entry.is_symlink() and entry.name not in RESERVED and entry.name not in names \
                and os.readlink(entry).startswith(".current/"):
            entry.unlink()
            links += 1
    return {"releases_removed": removed, "links_removed": links}


def status(root: Path = ROOT, state: Path = STATE) -> dict[str, Any]:
    saved = _load(state)
    live = current(root)
    return {
        "root": str(root),
        "current": live,
        "current_meta": saved["releases"].get(live or "", {}),
        "previous": saved.get("previous"),
        "staged": sorted(p.name for p in (root / ".releases").iterdir()
                         if RELEASE_ID.fullmatch(p.name)) if (root / ".releases").is_dir() else [],
        "mounted": OVERRIDE.is_file(),
    }


# ── one-time mount move (owner-approved activation) ──────────────────────────


def override_doc(root: Path) -> dict[str, Any]:
    return {"services": {"companion-core": {"volumes": [
        {"type": "bind", "source": str(root), "target": "/app/static", "read_only": True}]}}}


def activate(*, root: Path = ROOT, legacy: Path = LEGACY, override: Path = OVERRIDE,
             base: str = BASE, hold: float = 60, ready_timeout: float = 180) -> dict[str, Any]:
    """Point companion-core's /app/static at the web root: one recreate, rolled back on failure."""
    release = _release_module()
    live = current(root)
    if live is None:
        raise WebError("adopt the live web-build first")
    if inventory(tree(root, live)) != inventory(legacy):
        raise WebError("the web root's live release differs from the served web-build; adopt again")
    try:
        target = release.production_target()
    except release.ReleaseError as error:
        raise WebError(str(error)) from error
    image_id = target.inspect("{{.Image}}")
    if not image_id:
        raise WebError("companion-core is not running")
    doc: dict[str, Any] = {"command": "activate", "release": live, "image_id": image_id,
                           "started_at": datetime.now(UTC).isoformat()}
    try:
        staged = override.with_suffix(".tmp")
        override.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staged.write_text(json.dumps(override_doc(root), indent=2) + "\n")
        os.replace(staged, override)
        target.recreate()
        ok, detail = release.watch(target, image_id, ready_timeout=ready_timeout, hold=hold)
        if ok:
            doc["checks"] = smoke(base, root, live)
    except BaseException as error:  # noqa: BLE001 - restore on every escape
        ok, detail = False, f"{type(error).__name__}: {error}"[:300]
    doc["detail"] = detail
    if ok:
        doc["status"] = "activated"
    else:
        try:
            override.unlink(missing_ok=True)
            target.recreate()
            back, back_detail = release.watch(target, image_id, ready_timeout=ready_timeout, hold=30)
            doc.update(status="rolled_back" if back else "rollback_failed", rollback_detail=back_detail)
        except BaseException as error:  # noqa: BLE001
            doc.update(status="rollback_failed", rollback_detail=f"{type(error).__name__}: {error}"[:300])
    doc["finished_at"] = datetime.now(UTC).isoformat()
    return doc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("adopt")
    a.add_argument("--from", dest="source", type=Path, default=LEGACY)
    sub.add_parser("activate")
    d = sub.add_parser("deploy")
    d.add_argument("revision")
    r = sub.add_parser("rollback")
    r.add_argument("--to")
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(status(), indent=2, sort_keys=True))
        return 0
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(143))
    try:
        with klukai_deploy_lock.held(f"web {args.command}"):
            if args.command == "adopt":
                doc = adopt(args.source)
            elif args.command == "activate":
                doc = activate()
            elif args.command == "deploy":
                doc = deploy(args.revision)
            else:
                doc = rollback(args.to)
    except (WebError, InvalidEvidence, klukai_deploy_lock.Busy, OSError, ValueError) as error:
        doc = {"command": args.command, "status": "refused", "error": str(error)}
        print(json.dumps(doc, indent=2), file=sys.stderr)
        receipt(STATE, doc)
        return 1
    path = receipt(STATE, doc)
    print(json.dumps(doc, indent=2, sort_keys=True))
    print(f"receipt: {path}")
    return 0 if doc["status"] in SUCCESS[args.command] else 2


if __name__ == "__main__":
    sys.exit(main())
