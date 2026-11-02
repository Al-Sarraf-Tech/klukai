from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import pytest
from unittest import mock


REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ops/amarillo/scripts/klukai-fetch-ci-artifacts.py"
SPEC = importlib.util.spec_from_file_location("klukai_fetch_ci_artifacts", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
relay = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = relay
SPEC.loader.exec_module(relay)
from tools.ci import release_artifact  # noqa: E402

REV = "c6d02f0c884cd4585aaab96c28a13c32bbb6fc6f"
LAYER = "sha256:" + "a" * 64


class FakeAgent:
    """In-memory QEMU guest agent: files, find output and exec status."""

    def __init__(self, files: dict[str, bytes], listing: str = "") -> None:
        self.files, self.listing = files, listing
        self.handles: dict[int, list] = {}
        self.reads: list[str] = []

    def agent(self, command: dict) -> object:
        name, args = command["execute"], command.get("arguments", {})
        if name == "guest-file-open":
            if args["path"] not in self.files:
                raise RuntimeError("guest agent failed: no such file")
            self.reads.append(args["path"])
            handle = len(self.handles) + 1
            self.handles[handle] = [self.files[args["path"]], 0]
            return handle
        if name == "guest-file-read":
            data, offset = self.handles[args["handle"]]
            chunk = data[offset : offset + args["count"]]
            self.handles[args["handle"]][1] = offset + len(chunk)
            return {
                "buf-b64": base64.b64encode(chunk).decode(),
                "count": len(chunk),
                "eof": offset + len(chunk) >= len(data),
            }
        if name == "guest-file-close":
            self.handles.pop(args["handle"])
            return {}
        if name == "guest-exec":
            return {"pid": 7}
        if name == "guest-exec-status":
            out = base64.b64encode(self.listing.encode()).decode()
            return {"exited": True, "exitcode": 0, "out-data": out}
        raise AssertionError(name)


def materials() -> dict[str, bytes]:
    return {
        "sbom.cdx.json": json.dumps(
            {"bomFormat": "CycloneDX", "components": [{"name": "fastapi"}]}
        ).encode(),
        "licenses.json": b'{"Results": []}',
        "license-verdict.json": b'{"passed": true, "findings": 1}',
        "reproducibility.json": b'{"identical": false, "content_identical": true}',
    }


def manifest(
    revision: str = REV, payload: bytes = b"image-bytes", files: dict | None = None
) -> bytes:
    files = materials() if files is None else files
    return json.dumps(
        {
            "schema": release_artifact.SCHEMA,
            "revision": revision,
            "run": "9-1",
            "source_tree": "c" * 40,
            "tar_sha256": release_artifact.hashlib.sha256(payload).hexdigest(),
            "layers": [LAYER],
            "config_sha256": "e" * 64,
            "tools": {"trivy": "Version: 0.71.0"},
            "materials": {
                name: release_artifact.hashlib.sha256(body).hexdigest()
                for name, body in files.items()
            },
            "reproducibility": {"identical": False, "content_identical": True},
        }
    ).encode()


def release_files(base: str, revision: str = REV, **changes: bytes) -> dict:
    files = {**materials(), **changes}
    found = {f"{base}/{name}": body for name, body in files.items()}
    found[f"{base}/manifest.json"] = manifest(revision, files=materials())
    found[f"{base}/core-image.tar"] = b"image-bytes"
    return found


class GuestTests(unittest.TestCase):
    def test_multi_chunk_reads_reassemble_exactly_and_close_the_handle(self) -> None:
        payload = bytes(range(256)) * 100
        agent = FakeAgent({"/x": payload})
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(relay, "CHUNK", 1000),
        ):
            dest = Path(tmp) / "x"
            relay.Guest(agent).read("/x", dest)
            self.assertEqual(dest.read_bytes(), payload)
        self.assertEqual(agent.handles, {})

    def test_virsh_agent_runs_only_the_quoted_agent_command_over_ssh(self) -> None:
        completed = subprocess.CompletedProcess([], 0, '{"return": {"ok": 1}}', "")
        with mock.patch.object(relay.subprocess, "run", return_value=completed) as run:
            self.assertEqual(
                relay.VirshAgent("dominus-nobara", "fleet-ci-heavy-nobara").agent(
                    {"execute": "guest-ping"}
                ),
                {"ok": 1},
            )
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["ssh", "-o"])
        self.assertIn("qemu-agent-command fleet-ci-heavy-nobara", argv[-1])
        self.assertIn('\'{"execute": "guest-ping"}\'', argv[-1])

    def test_read_chunks_fit_libvirts_agent_reply_limit(self) -> None:
        # A full chunk, base64-encoded inside the JSON reply, must stay under
        # libvirt's cap, or the agent channel is disabled until the guest reconnects.
        reply = json.dumps(
            {
                "return": {
                    "buf-b64": base64.b64encode(b"\xff" * relay.CHUNK).decode(),
                    "count": relay.CHUNK,
                    "eof": False,
                }
            }
        )
        self.assertLess(len(reply), relay.LIBVIRT_AGENT_REPLY_MAX * 0.75)
        self.assertLess(len(reply), relay.LIBVIRT_RPC_STRING_MAX * 0.75)

    def test_ssh_connection_failure_is_reported_as_host_unreachable(self) -> None:
        down = subprocess.CompletedProcess([], 255, "", "No route to host")
        with mock.patch.object(relay.subprocess, "run", return_value=down):
            with self.assertRaises(relay.HostUnreachable):
                relay.VirshAgent("dominus-nobara", "d").agent({"execute": "guest-ping"})
        failed = subprocess.CompletedProcess([], 1, "", "agent not responding")
        with mock.patch.object(relay.subprocess, "run", return_value=failed):
            with self.assertRaises(RuntimeError) as caught:
                relay.VirshAgent("dominus-nobara", "d").agent({"execute": "guest-ping"})
        self.assertNotIsInstance(caught.exception, relay.HostUnreachable)

    def test_empty_read_before_eof_fails_instead_of_looping(self) -> None:
        agent = FakeAgent({"/x": b"data"})
        original = agent.agent

        def stuck(command: dict) -> object:
            if command["execute"] == "guest-file-read":
                return {"buf-b64": "", "count": 0, "eof": False}
            return original(command)

        agent.agent = stuck  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(RuntimeError):
            relay.Guest(agent).read("/x", Path(tmp) / "x")
        self.assertEqual(agent.handles, {})


class GreenRunTests(unittest.TestCase):
    def api(self, runs: list, jobs: dict) -> object:
        def call(path: str, token: str) -> dict:
            self.assertEqual(token, "tok")
            if "/jobs" in path:
                run_id = int(path.split("/runs/")[1].split("/")[0])
                rows = [{"id": index, "run_id": run_id, "head_sha": REV, "status": "completed", **job}
                        for index, job in enumerate(jobs[str(run_id)], 1)]
                return {"jobs": rows, "total_count": len(rows)}
            return {"workflow_runs": runs}

        return call

    def test_newest_run_with_required_gates_green_is_selected(self) -> None:
        runs = [
            {
                "id": 1,
                "run_attempt": 1,
                "head_sha": REV,
                "conclusion": "success",
                "status": "completed",
                "run_started_at": "2026-10-17T01:00:00Z",
            },
            {
                "id": 2,
                "run_attempt": 2,
                "head_sha": REV,
                "conclusion": "success",
                "status": "completed",
                "run_started_at": "2026-10-17T02:00:00Z",
            },
        ]
        jobs = {
            "1": [
                {"name": name, "conclusion": "success"} for name in relay.FULL_CI_GATES
            ],
            "2": [{"name": "Required CI gates", "conclusion": "skipped"}],
        }
        self.assertEqual(
            relay.green_run("o/r", REV, "tok", self.api(runs, jobs)), "1-1"
        )

    def test_component_suite_jobs_do_not_change_what_certifies(self) -> None:
        run = {"id": 1, "run_attempt": 1, "head_sha": REV, "conclusion": "success",
               "status": "completed", "run_started_at": "2026-10-17T01:00:00Z"}
        gates = [{"name": name, "conclusion": "success"} for name in relay.FULL_CI_GATES]
        shard = {"name": "Component suite (ops)", "conclusion": "success"}
        self.assertEqual(relay.green_run("o/r", REV, "tok", self.api([run], {"1": gates + [shard]})), "1-1")

    def test_no_green_gate_means_no_image(self) -> None:
        runs = [
            {
                "id": 3,
                "run_attempt": 1,
                "head_sha": REV,
                "conclusion": "success",
                "status": "completed",
                "run_started_at": "2026-10-17T01:00:00Z",
            }
        ]
        with self.assertRaises(ValueError):
            relay.green_run("o/r", REV, "tok", self.api(runs, {"3": []}))


class FetchImageTests(unittest.TestCase):
    def test_verified_image_is_loaded_from_the_exact_run_directory(self) -> None:
        base = f"/opt/klukai-ci/artifacts/{REV}/9-1/release"
        agent = FakeAgent(release_files(base))
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                release_artifact, "verify", return_value={"tag": "ok"}
            ) as verify,
        ):
            result = relay.fetch_image(relay.Guest(agent), "9-1", REV, Path(tmp))
        self.assertEqual(result["tag"], "ok")
        self.assertEqual(result["sbom_components"], 1)
        self.assertEqual(verify.call_args.args[3], f"klukai-core:ci-{REV[:7]}")

    def test_manifest_from_another_run_or_unlogged_digest_is_refused(self) -> None:
        base = f"/opt/klukai-ci/artifacts/{REV}/9-1/release"
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            relay.fetch_image(
                relay.Guest(FakeAgent(release_files(base))),
                "9-1",
                REV,
                Path(tmp),
                manifest_sha256="0" * 64,
            )
        other = f"/opt/klukai-ci/artifacts/{REV}/8-1/release"
        files = {k.replace(base, other): v for k, v in release_files(base).items()}
        agent = FakeAgent(files)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            relay.fetch_image(relay.Guest(agent), "8-1", REV, Path(tmp))
        self.assertNotIn(f"{other}/core-image.tar", agent.reads)

    def test_the_ci_logged_digest_is_read_from_the_runs_own_scan_job(self) -> None:
        digest = "a" * 64
        common = {"run_id": 9, "head_sha": REV, "status": "completed"}
        jobs = {
            "total_count": 2,
            "jobs": [
                {**common, "id": 5, "name": "Lint", "conclusion": "success"},
                {**common, "id": 7, "name": "Image Scan (trivy)", "conclusion": "success"},
            ]
        }
        seen = {}

        def api(path: str, token: str) -> dict:
            seen["path"] = path
            return jobs

        def log(repo: str, job_id: int, token: str) -> str:
            seen["job"] = job_id
            return (
                'echo "KLUKAI_RELEASE_MANIFEST_SHA256=$(sha256sum ...)"\n'
                f"KLUKAI_RELEASE_MANIFEST_SHA256={digest}\n"
            )

        self.assertEqual(
            relay.ci_manifest_digest("o/r", "9-2", "tok", api, log, revision=REV), digest
        )
        self.assertIn("/runs/9/attempts/2/jobs", seen["path"])
        self.assertEqual(seen["job"], 7)
        with self.assertRaises(ValueError):
            relay.ci_manifest_digest("o/r", "9-2", "tok", api, lambda *a: "nothing", revision=REV)

    def test_tampered_material_stops_before_the_image_transfer(self) -> None:
        base = f"/opt/klukai-ci/artifacts/{REV}/9-1/release"
        agent = FakeAgent(
            release_files(base, **{"license-verdict.json": b'{"passed": false}'})
        )
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            relay.fetch_image(relay.Guest(agent), "9-1", REV, Path(tmp))
        self.assertNotIn(f"{base}/core-image.tar", agent.reads)

    def test_provenance_is_signed_only_after_verification_and_verifies(self) -> None:
        base = f"/opt/klukai-ci/artifacts/{REV}/9-1/release"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / "release_ed25519"
            subprocess.run(
                [
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-C",
                    "test",
                    "-f",
                    str(key),
                ],
                check=True,
            )
            dest = root / "dest"
            with mock.patch.object(
                release_artifact, "verify", return_value={"tag": "ok"}
            ):
                result = relay.fetch_image(
                    relay.Guest(FakeAgent(release_files(base))),
                    "9-1",
                    REV,
                    dest,
                    key=key,
                )
            signers = root / "allowed_signers"
            signers.write_text(
                'klukai-release@amarillo namespaces="klukai-release" '
                + (root / "release_ed25519.pub").read_text()
            )
            provenance = dest / "provenance.json"
            verify = subprocess.run(
                [
                    "ssh-keygen",
                    "-Y",
                    "verify",
                    "-f",
                    str(signers),
                    "-I",
                    "klukai-release@amarillo",
                    "-n",
                    "klukai-release",
                    "-s",
                    result["signature"],
                ],
                input=provenance.read_bytes(),
                capture_output=True,
            )
            self.assertEqual(verify.returncode, 0, verify.stderr)
            doc = json.loads(provenance.read_text())
            self.assertEqual(doc["ci_run"], "9-1")
            self.assertEqual(
                doc["manifest_sha256"],
                release_artifact.sha256(dest / "manifest.json"),
            )
            # A failed verification never leaves a fresh signature behind.
            with (
                mock.patch.object(
                    release_artifact, "verify", side_effect=ValueError("x")
                ),
                self.assertRaises(ValueError),
            ):
                relay.fetch_image(
                    relay.Guest(FakeAgent(release_files(base))),
                    "9-1",
                    REV,
                    dest,
                    key=key,
                )
            self.assertFalse((dest / "provenance.json.sig").exists())

    def test_manifest_for_another_revision_stops_before_the_image_transfer(
        self,
    ) -> None:
        base = f"/opt/klukai-ci/artifacts/{REV}/9-1/release"
        other = "5d9c3f054d9f1a6c56da091a7f9bd3fbffc1df27"
        agent = FakeAgent(release_files(base, revision=other))
        agent.files[f"{base}/manifest.json"] = manifest(other)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            relay.fetch_image(relay.Guest(agent), "9-1", REV, Path(tmp))
        self.assertNotIn(f"{base}/core-image.tar", agent.reads)

    def test_run_identity_cannot_escape_the_artifact_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            relay.fetch_image(relay.Guest(FakeAgent({})), "../../etc", REV, Path(tmp))

    def test_revision_must_be_a_full_commit_before_any_transfer(self) -> None:
        agent = FakeAgent({})
        for bad in ("../../etc", REV[:7], REV.upper()):
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
                relay.fetch_image(relay.Guest(agent), "9-1", bad, Path(tmp))
        self.assertEqual(agent.reads, [])


class NightlyTests(unittest.TestCase):
    def summary(self, finished: str) -> bytes:
        return json.dumps(
            {
                "revision": REV,
                "finished_at": finished,
                "routes": [{"route": "/health", "candidate_p99_ms": 1.2}],
            }
        ).encode()

    def test_new_valid_summaries_reach_the_sink_once(self) -> None:
        good = "/opt/klukai-ci/artifacts/nightly/2026-10-17T01-30Z-c6d02f0/summary.json"
        bad = "/opt/klukai-ci/artifacts/nightly/2026-10-18T01-30Z-c6d02f0/summary.json"
        agent = FakeAgent(
            {
                good: self.summary("2026-10-17T01:45:00+00:00"),
                bad: b'{"revision": "x"}',
            },
            listing=f"{good}\n{bad}\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            state, sink = Path(tmp) / "state", Path(tmp) / "sink.jsonl"
            state.mkdir()
            added = relay.fetch_nightly(relay.Guest(agent), state, sink)
            self.assertEqual(added, ["2026-10-17T01-30Z-c6d02f0"])
            night, route = (json.loads(x) for x in sink.read_text().splitlines())
            self.assertEqual(
                (night["kind"], night["source"]), ("night", "klukai-perf-nightly")
            )
            self.assertEqual((route["kind"], route["route"]), ("route", "/health"))
            self.assertEqual(route["candidate_p99_ms"], 1.2)
            self.assertEqual(relay.fetch_nightly(relay.Guest(agent), state, sink), [])
            self.assertEqual(len(sink.read_text().splitlines()), 2)
            now = datetime(2026, 10, 17, 2, tzinfo=UTC)
            self.assertFalse(relay.stale(state, timedelta(days=2), now))
            self.assertTrue(
                relay.stale(state, timedelta(days=2), now + timedelta(days=3))
            )

    def test_corrupt_summary_is_skipped_without_blocking_later_nights(self) -> None:
        root = "/opt/klukai-ci/artifacts/nightly"
        torn = f"{root}/2026-10-17T01-30Z-c6d02f0/summary.json"
        good = f"{root}/2026-10-18T01-30Z-c6d02f0/summary.json"
        agent = FakeAgent(
            {torn: b'{"revision": "', good: self.summary("2026-10-18T01:45:00+00:00")},
            listing=f"{torn}\n{good}\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            state, sink = Path(tmp) / "state", Path(tmp) / "sink.jsonl"
            state.mkdir()
            added = relay.fetch_nightly(relay.Guest(agent), state, sink)
            self.assertEqual(added, ["2026-10-18T01-30Z-c6d02f0"])
            # The torn file is not marked seen, so a completed copy is picked up later.
            self.assertNotIn("2026-10-17T01-30Z", (state / "seen.txt").read_text())

    def test_an_interrupted_transfer_leaves_nothing_behind(self) -> None:
        # Independent review finding 7: a torn download used to stay in the
        # state directory and crash the freshness scan before the owner warning.
        path = "/opt/klukai-ci/artifacts/nightly/2026-10-17T01-30Z-c6d02f0/summary.json"

        class Dropping(FakeAgent):
            def agent(inner, command: dict) -> object:
                if command["execute"] == "guest-file-read":
                    data, _ = inner.handles[command["arguments"]["handle"]]
                    if inner.handles[command["arguments"]["handle"]][1] == 0:
                        inner.handles[command["arguments"]["handle"]][1] = 10
                        return {
                            "buf-b64": base64.b64encode(data[:10]).decode(),
                            "count": 10,
                            "eof": False,
                        }
                    raise RuntimeError("ssh: connection to Dominus lost")
                return super().agent(command)

        agent = Dropping(
            {path: self.summary("2026-10-17T01:45:00+00:00")}, listing=f"{path}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            state, sink = Path(tmp) / "state", Path(tmp) / "sink.jsonl"
            state.mkdir()
            with self.assertRaises(RuntimeError):
                relay.fetch_nightly(relay.Guest(agent), state, sink)
            self.assertEqual(sorted(p.name for p in state.iterdir()), [])
            self.assertTrue(
                relay.stale(
                    state, timedelta(days=2), datetime(2026, 10, 20, tzinfo=UTC)
                )
            )

    def test_the_freshness_scan_survives_a_corrupt_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "2026-10-16T01-30Z-c6d02f0.json").write_text('{"revision": "')
            (state / "2026-10-17T01-30Z-c6d02f0.json").write_bytes(
                self.summary("2026-10-17T01:45:00+00:00")
            )
            now = datetime(2026, 10, 18, tzinfo=UTC)
            self.assertFalse(relay.stale(state, timedelta(days=2), now))
            self.assertTrue(
                relay.stale(state, timedelta(days=2), now + timedelta(days=3))
            )

    def test_finished_at_must_carry_a_timezone(self) -> None:
        doc = json.loads(self.summary("2026-10-17T01:45:00+00:00"))
        self.assertTrue(relay.valid_summary(doc))
        for bad in ("2026-10-17T01:45:00", "yesterday", None):
            self.assertFalse(relay.valid_summary({**doc, "finished_at": bad}))

    def test_missing_summary_warning_is_sent_at_most_once_a_day(self) -> None:
        now = datetime(2026, 10, 20, 12, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.assertTrue(relay.should_warn(state, now))
            self.assertFalse(relay.should_warn(state, now + timedelta(hours=23)))
            self.assertTrue(relay.should_warn(state, now + timedelta(hours=25)))


class ReleaseManifestTests(unittest.TestCase):
    def test_manifest_must_match_the_revision_and_carry_layers(self) -> None:
        good = json.loads(manifest())
        release_artifact.check_manifest(good, REV)
        for broken in (
            {**good, "schema": 1},
            {**good, "layers": []},
            {**good, "tar_sha256": "x"},
            {**good, "materials": {}},
            {**good, "tools": {}},
            {**good, "source_tree": "short"},
            {**good, "config_sha256": None},
        ):
            with self.assertRaises(ValueError):
                release_artifact.check_manifest(broken, REV)
        with self.assertRaises(ValueError):
            release_artifact.check_manifest(good, "short")


if __name__ == "__main__":
    unittest.main()


def test_routine_or_incomplete_success_never_certifies_a_release():
    run = {
        "id": 1,
        "run_attempt": 1,
        "head_sha": REV,
        "conclusion": "success",
        "status": "completed",
        "run_started_at": "2026-10-17T01:00:00Z",
    }
    jobs = [
        {"id": index, "run_id": 1, "head_sha": REV, "status": "completed",
         "name": name, "conclusion": "success"}
        for index, name in enumerate(["Affected CI gates", "Required CI gates"], 1)
    ]

    def api(path, token):
        return {"jobs": jobs, "total_count": len(jobs)} if "/jobs" in path else {"workflow_runs": [run]}

    with pytest.raises(ValueError, match="successful CI run"):
        relay.green_run("o/r", REV, "tok", api)


def test_even_fully_green_ci_for_another_revision_is_rejected():
    run = {
        "id": 1,
        "run_attempt": 1,
        "head_sha": "b" * 40,
        "conclusion": "success",
        "status": "completed",
        "run_started_at": "2026-10-17T01:00:00Z",
    }

    def api(path, token):
        if "/jobs" in path:
            raise AssertionError("a different revision must be refused before reading its jobs")
        return {"workflow_runs": [run]}

    with pytest.raises(ValueError):
        relay.green_run("o/r", REV, "tok", api)


def test_certification_does_not_mix_a_run_snapshot_with_a_later_attempt():
    """A rerun starting between API calls cannot certify the earlier artifacts."""
    run = {"id": 1, "run_attempt": 1, "head_sha": REV, "conclusion": "success",
           "status": "completed", "run_started_at": "2026-10-17T01:00:00Z"}
    full = [{"id": index, "run_id": 1, "head_sha": REV, "status": "completed",
             "name": name, "conclusion": "success"}
            for index, name in enumerate(sorted(relay.FULL_CI_GATES), 1)]
    earlier = [{**job, "conclusion": "skipped"} if job["name"] == "Required CI gates" else job
               for job in full]

    def api(path, token):
        if "/jobs" not in path:
            return {"workflow_runs": [run]}
        if "/attempts/1/jobs" in path:
            return {"jobs": earlier, "total_count": len(earlier)}
        return {"jobs": full, "total_count": len(full)}  # unpinned endpoint now returns attempt 2

    with pytest.raises(ValueError, match="successful CI run"):
        relay.green_run("o/r", REV, "tok", api)


def test_certification_reads_the_pinned_attempt_even_when_the_latest_is_pending():
    run = {"id": 1, "run_attempt": 1, "head_sha": REV, "conclusion": "success",
           "status": "completed", "run_started_at": "2026-10-17T01:00:00Z"}

    def api(path, token):
        if "/jobs" not in path:
            return {"workflow_runs": [run]}
        if "/attempts/1/jobs" in path:
            return {"jobs": [{"id": index, "run_id": 1, "head_sha": REV, "status": "completed",
                              "name": name, "conclusion": "success"}
                             for index, name in enumerate(sorted(relay.FULL_CI_GATES), 1)],
                    "total_count": len(relay.FULL_CI_GATES)}
        return {"jobs": [], "total_count": 0}  # attempt 2 has just started

    assert relay.green_run("o/r", REV, "tok", api) == "1-1"


def certifying_run():
    return {"id": 9, "run_attempt": 2, "head_sha": REV, "conclusion": "success",
            "status": "completed", "run_started_at": "2026-10-17T01:00:00Z"}


def certifying_jobs():
    return [{"id": index, "run_id": 9, "run_attempt": 2, "head_sha": REV,
             "name": name, "status": "completed", "conclusion": "success"}
            for index, name in enumerate(sorted(relay.FULL_CI_GATES), 1)]


@pytest.mark.parametrize("field,value", [
    ("run_id", 10), ("run_attempt", 1), ("head_sha", "b" * 40),
    ("run_id", None), ("run_attempt", None), ("head_sha", None),
    ("status", "in_progress"), ("conclusion", "skipped"),
])
def test_gate_evidence_must_match_the_complete_exact_run_attempt(field, value):
    jobs = certifying_jobs()
    jobs[0][field] = value

    def api(path, token):
        return {"jobs": jobs, "total_count": len(jobs)} if "/jobs" in path else {
            "workflow_runs": [certifying_run()]}

    with pytest.raises(ValueError, match="successful CI run"):
        relay.green_run("o/r", REV, "tok", api)


@pytest.mark.parametrize("total", [None, 0, len(relay.FULL_CI_GATES) + 1])
def test_an_incomplete_gate_response_cannot_certify_a_run(total):
    jobs = certifying_jobs()

    def api(path, token):
        return {"jobs": jobs, "total_count": total} if "/jobs" in path else {
            "workflow_runs": [certifying_run()]}

    with pytest.raises(ValueError, match="successful CI run"):
        relay.green_run("o/r", REV, "tok", api)


def test_matching_complete_evidence_certifies_the_exact_attempt():
    jobs = certifying_jobs()

    def api(path, token):
        return {"jobs": jobs, "total_count": len(jobs)} if "/jobs" in path else {
            "workflow_runs": [certifying_run()]}

    assert relay.green_run("o/r", REV, "tok", api) == "9-2"


def test_the_pinned_endpoint_does_not_require_the_optional_job_attempt_field():
    jobs = certifying_jobs()
    for job in jobs:
        del job["run_attempt"]

    def api(path, token):
        return {"jobs": jobs, "total_count": len(jobs)} if "/jobs" in path else {
            "workflow_runs": [certifying_run()]}

    assert relay.green_run("o/r", REV, "tok", api) == "9-2"


def paginated_jobs_api(jobs, *, second_page=None, second_total=None):
    def api(path, token):
        if "/jobs" not in path:
            return {"workflow_runs": [certifying_run()]}
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(path).query)
        page, size = int(query.get("page", ["1"])[0]), int(query["per_page"][0])
        if page == 2 and second_page is not None:
            return {"jobs": second_page, "total_count": len(jobs) if second_total is None else second_total}
        return {"jobs": jobs[(page - 1) * size:page * size], "total_count": len(jobs)}
    return api


def many_certifying_jobs():
    jobs = certifying_jobs()
    return [{**jobs[0], "id": 1000 + index, "name": f"extra-{index}"} for index in range(100)] + jobs


def test_all_pages_of_the_exact_attempt_are_checked_before_certifying():
    assert relay.green_run("o/r", REV, "tok", paginated_jobs_api(many_certifying_jobs())) == "9-2"


@pytest.mark.parametrize("fault", ["empty", "duplicate", "changed-total"])
def test_torn_or_repeated_job_pages_cannot_certify(fault):
    jobs = many_certifying_jobs()
    if fault == "empty":
        api = paginated_jobs_api(jobs, second_page=[])
    elif fault == "duplicate":
        api = paginated_jobs_api(jobs, second_page=jobs[:len(relay.FULL_CI_GATES)])
    else:
        api = paginated_jobs_api(jobs, second_page=jobs[100:], second_total=len(jobs) + 1)
    with pytest.raises(ValueError):
        relay.green_run("o/r", REV, "tok", api)


def test_an_ambiguous_gate_name_cannot_hide_a_failed_copy():
    jobs = certifying_jobs()
    jobs.append({**jobs[0], "id": 100, "conclusion": "failure"})
    with pytest.raises(ValueError):
        relay.green_run("o/r", REV, "tok", paginated_jobs_api(jobs))


@pytest.mark.parametrize("name", sorted(relay.FULL_CI_GATES))
def test_every_full_ci_gate_is_required(name):
    jobs = [job for job in certifying_jobs() if job["name"] != name]
    with pytest.raises(ValueError):
        relay.green_run("o/r", REV, "tok", paginated_jobs_api(jobs))


@pytest.mark.parametrize("field", ["id", "run_id", "head_sha", "name", "status", "conclusion"])
def test_a_required_job_field_cannot_be_missing(field):
    jobs = certifying_jobs()
    del jobs[0][field]
    with pytest.raises(ValueError):
        relay.green_run("o/r", REV, "tok", paginated_jobs_api(jobs))


@pytest.mark.parametrize("response", [None, {}, {"workflow_runs": None}, {"workflow_runs": [None]}])
def test_incomplete_run_responses_fail_closed(response):
    with pytest.raises(ValueError):
        relay.green_run("o/r", REV, "tok", lambda *args: response)
