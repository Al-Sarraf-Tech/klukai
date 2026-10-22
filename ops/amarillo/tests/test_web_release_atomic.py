"""Atomic web releases: stage, switch, verify through the server, roll back."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "ops/amarillo/scripts"))
sys.path.insert(0, str(REPO))
import klukai_web_release as web  # noqa: E402
from tools.ci import web_release  # noqa: E402
from tools.ci.frontend_evidence import inventory  # noqa: E402

REV_A, REV_B = "a" * 40, "b" * 40


def bundle(path: Path, label: str) -> Path:
    """A CI-built PWA, content-hashed exactly as the relay does it."""
    path.mkdir(parents=True)
    (path / "main.dart.js").write_text(f"main {label}\n")
    (path / "flutter_bootstrap.js").write_text('{"mainJsPath":"main.dart.js"};c("main.dart.js");\n')
    (path / "index.html").write_text('<script src="flutter_bootstrap.js"></script>\n')
    (path / "login.html").write_text(f"login {label}\n")
    (path / "flutter.js").write_text("flutter\n")
    (path / "assets").mkdir()
    (path / "assets" / "AssetManifest.json").write_text(f'["{label}"]\n')
    web_release.content_hash(path)
    return path


@pytest.fixture
def signer(tmp_path):
    key = tmp_path / "keys" / "release"
    key.parent.mkdir()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(key)], check=True)
    signers = tmp_path / "keys" / "allowed_signers"
    signers.write_text('klukai-release@amarillo namespaces="klukai-release" ' + key.with_suffix(".pub").read_text())
    return key, signers


def relayed(releases: Path, revision: str, label: str, key: Path, *, kind: str = "klukai-web") -> Path:
    """What klukai-fetch-ci-artifacts.py `web` leaves for the deployer."""
    directory = releases / revision / "web"
    release = bundle(directory / "release", label)
    doc = {"schema": 1, "kind": kind, "revision": revision, "ci_run": "7-1",
           "ci_pwa_sha256": "c" * 64, "entry": {}, "release": inventory(release)}
    (directory / "web-provenance.json").write_text(json.dumps(doc))
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", "klukai-release",
                    str(directory / "web-provenance.json")], check=True)
    return directory


class Server:
    """Starlette StaticFiles semantics: realpath must stay inside the root."""

    def __init__(self, root: Path, broken: set[str] | None = None) -> None:
        self.root, self.broken, self.calls = root, broken or set(), []

    def __call__(self, url: str):
        path = url.split("8300", 1)[1]
        self.calls.append(path)
        name = "login.html" if path == "/" else ("index.html" if path == "/app/" else path.removeprefix("/app/"))
        full = os.path.realpath(self.root / name)
        if os.path.commonpath([full, os.path.realpath(self.root)]) != os.path.realpath(self.root) \
                or not os.path.isfile(full):
            return 404, b"", {}
        body = Path(full).read_bytes()
        if name in self.broken:
            body = b"stale"
        cache = web.IMMUTABLE if web.HASHED.fullmatch(name) else "no-cache"
        return 200, body, {"Cache-Control": cache}


@pytest.fixture
def site(tmp_path, signer):
    key, signers = signer
    root, state, releases = tmp_path / "root", tmp_path / "state", tmp_path / "releases"
    live = bundle(tmp_path / "web-build", "live")
    adopted = web.adopt(live, root=root, state=state)
    kw = dict(root=root, releases=releases, state=state, signers=signers, base="http://127.0.0.1:8300")
    return {"root": root, "state": state, "releases": releases, "key": key, "kw": kw,
            "adopted": adopted["release"], "live": live}


def served(root: Path, name: str) -> bytes:
    return (root / name).read_bytes()


def test_adopt_serves_the_same_bytes_without_changing_the_source(site):
    root = site["root"]
    assert web.current(root) == site["adopted"]
    assert inventory(web.tree(root, site["adopted"])) == inventory(site["live"])
    assert served(root, "login.html") == b"login live\n"
    assert os.readlink(root / "index.html") == ".current/index.html"
    with pytest.raises(web.WebError):
        web.adopt(site["live"], root=root, state=site["state"])


def test_a_verified_release_is_switched_in_and_checked_through_the_server(site):
    relayed(site["releases"], REV_B, "new", site["key"])
    server = Server(site["root"])
    doc = web.deploy(REV_B, fetch=server, **site["kw"])
    assert doc["status"] == "deployed", doc
    assert web.current(site["root"]) == doc["release"]
    assert served(site["root"], "login.html") == b"login new\n"
    assert "/app/" in server.calls and "/" in server.calls
    saved = json.loads((site["state"] / "state.json").read_text())
    assert saved["previous"] == site["adopted"] and saved["current"] == doc["release"]


def test_the_previous_entry_bundles_stay_reachable_after_the_switch(site):
    old = web.entry_files(web.tree(site["root"], site["adopted"]))
    relayed(site["releases"], REV_B, "new", site["key"])
    doc = web.deploy(REV_B, fetch=Server(site["root"]), **site["kw"])
    assert sorted(old) == doc["carried"]
    for name, sha in old.items():
        assert hashlib.sha256(served(site["root"], name)).hexdigest() == sha


def test_a_later_commit_with_the_same_pwa_changes_nothing(site):
    relayed(site["releases"], REV_B, "new", site["key"])
    web.deploy(REV_B, fetch=Server(site["root"]), **site["kw"])
    live = web.current(site["root"])
    relayed(site["releases"], "c" * 40, "new", site["key"])
    again = web.deploy("c" * 40, fetch=Server(site["root"]), **site["kw"])
    assert again["status"] == "unchanged" and web.current(site["root"]) == live


def test_redeploying_the_live_release_changes_nothing(site):
    relayed(site["releases"], REV_B, "new", site["key"])
    web.deploy(REV_B, fetch=Server(site["root"]), **site["kw"])
    again = web.deploy(REV_B, fetch=Server(site["root"]), **site["kw"])
    assert again["status"] == "unchanged"


@pytest.mark.parametrize("fault", ["unsigned", "wrong-signer", "wrong-kind", "other-revision", "tampered-file"])
def test_an_invalid_artifact_is_refused_before_anything_is_served(site, tmp_path, fault):
    directory = relayed(site["releases"], REV_B, "new", site["key"],
                        kind="klukai-core" if fault == "wrong-kind" else "klukai-web")
    if fault == "unsigned":
        (directory / "web-provenance.json.sig").unlink()
    elif fault == "wrong-signer":
        other = tmp_path / "other"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
        (directory / "web-provenance.json.sig").unlink()
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(other), "-n", "klukai-release",
                        str(directory / "web-provenance.json")], check=True)
    elif fault == "tampered-file":
        (directory / "release" / "login.html").write_text("evil\n")
    revision = REV_A if fault == "other-revision" else REV_B
    if fault == "other-revision":
        (site["releases"] / REV_A).mkdir()
        os.rename(directory, site["releases"] / REV_A / "web")
    before = os.readlink(site["root"] / ".current")
    with pytest.raises(web.WebError):
        web.deploy(revision, fetch=Server(site["root"]), **site["kw"])
    assert os.readlink(site["root"] / ".current") == before


def test_a_failed_check_switches_back_and_verifies_the_previous_release(site):
    relayed(site["releases"], REV_B, "new", site["key"])
    doc = web.deploy(REV_B, fetch=Server(site["root"], broken={"login.html"}), **site["kw"])
    # The broken server breaks login.html for both releases, so the restore
    # check fails too: the live release is back but reported loudly.
    assert doc["status"] == "rollback_failed"
    assert web.current(site["root"]) == site["adopted"]


def test_a_check_failing_only_for_the_new_release_rolls_back_cleanly(site):
    relayed(site["releases"], REV_B, "new", site["key"])
    new_main = next(n for n in inventory(site["releases"] / REV_B / "web" / "release")["files"]
                    if n.startswith("main.dart."))
    doc = web.deploy(REV_B, fetch=Server(site["root"], broken={new_main}), **site["kw"])
    assert doc["status"] == "rolled_back", doc
    assert web.current(site["root"]) == site["adopted"]
    assert served(site["root"], "login.html") == b"login live\n"


def test_an_interrupt_during_the_checks_still_switches_back(site):
    relayed(site["releases"], REV_B, "new", site["key"])
    server = Server(site["root"])

    def interrupted(url):
        if web.current(site["root"]) != site["adopted"]:
            raise KeyboardInterrupt
        return server(url)

    doc = web.deploy(REV_B, fetch=interrupted, **site["kw"])
    assert doc["status"] == "rolled_back"
    assert web.current(site["root"]) == site["adopted"]


def test_rollback_returns_to_the_previous_release(site):
    relayed(site["releases"], REV_B, "new", site["key"])
    web.deploy(REV_B, fetch=Server(site["root"]), **site["kw"])
    doc = web.rollback(root=site["root"], state=site["state"], base="http://127.0.0.1:8300",
                       fetch=Server(site["root"]))
    assert doc["status"] == "rolled_back"
    assert web.current(site["root"]) == site["adopted"]
    with pytest.raises(web.WebError):
        web.rollback("aaaaaaaaaaaa-bbbbbbbbbbbb", root=site["root"], state=site["state"],
                     fetch=Server(site["root"]))


def test_a_retry_reuses_the_verified_staging_and_cleans_a_torn_copy(site):
    directory = relayed(site["releases"], REV_B, "new", site["key"])
    doc = json.loads((directory / "web-provenance.json").read_text())
    rid = web.release_id(REV_B, doc["release"]["sha256"])
    torn = site["root"] / ".releases" / f".{rid}.partial"
    torn.mkdir(parents=True)
    (torn / "half").write_text("x")
    first = web.stage(site["root"], directory / "release", rid, doc["release"])
    mtime = first.stat().st_mtime_ns
    second = web.stage(site["root"], directory / "release", rid, doc["release"])
    assert first == second and second.stat().st_mtime_ns == mtime
    assert not torn.exists()


def test_prune_keeps_live_previous_and_newest(site):
    for index, revision in enumerate(["1" * 40, "2" * 40, "3" * 40, "4" * 40]):
        relayed(site["releases"], revision, f"r{index}", site["key"])
        assert web.deploy(revision, fetch=Server(site["root"]), keep=2, **site["kw"])["status"] == "deployed"
    staged = web.status(site["root"], site["state"])["staged"]
    saved = json.loads((site["state"] / "state.json").read_text())
    assert saved["current"] in staged and saved["previous"] in staged
    assert len(staged) == 2


def test_readers_never_miss_a_bundle_across_a_forward_switch(site):
    """A browser reads index.html, then the bootstrap it names. Across one
    forward switch (a deploy), that bootstrap is always still served."""
    revisions = [f"{i:x}" * 40 for i in range(1, 9)]
    for index, revision in enumerate(revisions):
        relayed(site["releases"], revision, f"r{index}", site["key"])
    switches, errors, stop = [0], [], threading.Event()
    real_switch = web.switch

    def counted(root, rid):
        real_switch(root, rid)
        switches[0] += 1

    def reader():
        while not stop.is_set():
            before = switches[0]
            page = (site["root"] / "index.html").read_text()
            boot = page.split('src="', 1)[1].split('"', 1)[0]
            try:
                (site["root"] / boot).read_bytes()
            except FileNotFoundError:
                if switches[0] - before <= 1:
                    errors.append(boot)

    web.switch = counted
    threads = [threading.Thread(target=reader) for _ in range(4)]
    try:
        for t in threads:
            t.start()
        for revision in revisions:
            assert web.deploy(revision, fetch=Server(site["root"]), keep=20, **site["kw"])["status"] == "deployed"
    finally:
        stop.set()
        for t in threads:
            t.join()
        web.switch = real_switch
    assert switches[0] >= len(revisions)
    assert errors == []


def test_switching_is_one_atomic_rename(site):
    """Every reader sees index.html of exactly one release, never a torn file."""
    relayed(site["releases"], REV_B, "new", site["key"])
    web.deploy(REV_B, fetch=Server(site["root"]), **site["kw"])
    new = web.current(site["root"])
    pages = {(web.tree(site["root"], rid) / "index.html").read_text() for rid in (new, site["adopted"])}
    seen, stop = [], threading.Event()

    def reader():
        while not stop.is_set():
            seen.append((site["root"] / "index.html").read_text())

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for _ in range(300):
        web.switch(site["root"], site["adopted"])
        web.switch(site["root"], new)
    stop.set()
    for t in threads:
        t.join()
    assert seen and set(seen) <= pages


def test_reserved_or_linked_names_are_refused(site, tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / ".current").write_text("x")
    with pytest.raises(web.WebError):
        web._names(bad)
    (site["root"] / "rogue").write_text("real file")
    with pytest.raises(web.WebError):
        web.ensure_links(site["root"], ["rogue"])


def test_activation_override_moves_only_the_static_mount(tmp_path):
    doc = web.override_doc(tmp_path / "root")
    volumes = doc["services"]["companion-core"]["volumes"]
    assert volumes == [{"type": "bind", "source": str(tmp_path / "root"),
                        "target": "/app/static", "read_only": True}]


def test_a_reset_connection_is_a_retried_attempt_not_a_crash(site, monkeypatch):
    relayed(site["releases"], REV_B, "new", site["key"])
    server, resets = Server(site["root"]), [2]
    monkeypatch.setattr(web.time, "sleep", lambda s: None)

    def flaky(url):
        if resets[0]:
            resets[0] -= 1
            raise ConnectionResetError(104, "reset")
        return server(url)

    assert web.deploy(REV_B, fetch=flaky, **site["kw"])["status"] == "deployed"


def test_the_deployers_restore_rechecks_a_release_that_is_already_live(site):
    live = web.current(site["root"])
    doc = web.rollback(live, root=site["root"], state=site["state"], base="http://127.0.0.1:8300",
                       fetch=Server(site["root"]), allow_live=True)
    assert doc["status"] == "rolled_back" and doc["checks"]
    broken = web.rollback(live, root=site["root"], state=site["state"], base="http://127.0.0.1:8300",
                          fetch=Server(site["root"], broken={"login.html"}), allow_live=True)
    assert broken["status"] == "rollback_failed"
    with pytest.raises(web.WebError):  # the owner's command still refuses a no-op
        web.rollback(live, root=site["root"], state=site["state"], fetch=Server(site["root"]))


def test_a_failure_recording_the_switch_switches_back(site, monkeypatch):
    relayed(site["releases"], REV_B, "new", site["key"])
    real = web._record
    calls = []

    def failing(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise OSError(28, "No space left on device")
        return real(*args, **kwargs)

    monkeypatch.setattr(web, "_record", failing)
    doc = web.deploy(REV_B, fetch=Server(site["root"]), **site["kw"])
    assert doc["status"] == "rolled_back"
    assert web.current(site["root"]) == site["adopted"]
