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


def manifest(revision: str = REV, payload: bytes = b"image-bytes") -> bytes:
    return json.dumps(
        {
            "schema": 1,
            "revision": revision,
            "tar_sha256": release_artifact.hashlib.sha256(payload).hexdigest(),
            "layers": [LAYER],
        }
    ).encode()


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


class GreenRunTests(unittest.TestCase):
    def api(self, runs: list, jobs: dict) -> object:
        def call(path: str, token: str) -> dict:
            self.assertEqual(token, "tok")
            if "/jobs" in path:
                return {"jobs": jobs[path.split("/runs/")[1].split("/")[0]]}
            return {"workflow_runs": runs}

        return call

    def test_newest_run_with_required_gates_green_is_selected(self) -> None:
        runs = [
            {
                "id": 1,
                "run_attempt": 1,
                "head_sha": REV,
                "conclusion": "success",
                "run_started_at": "2026-10-17T01:00:00Z",
            },
            {
                "id": 2,
                "run_attempt": 2,
                "head_sha": REV,
                "conclusion": "success",
                "run_started_at": "2026-10-17T02:00:00Z",
            },
        ]
        jobs = {
            "1": [{"name": "Required CI gates", "conclusion": "success"}],
            "2": [{"name": "Required CI gates", "conclusion": "skipped"}],
        }
        self.assertEqual(
            relay.green_run("o/r", REV, "tok", self.api(runs, jobs)), "1-1"
        )

    def test_no_green_gate_means_no_image(self) -> None:
        runs = [
            {
                "id": 3,
                "run_attempt": 1,
                "head_sha": REV,
                "conclusion": "success",
                "run_started_at": "2026-10-17T01:00:00Z",
            }
        ]
        with self.assertRaises(ValueError):
            relay.green_run("o/r", REV, "tok", self.api(runs, {"3": []}))


class FetchImageTests(unittest.TestCase):
    def test_verified_image_is_loaded_from_the_exact_run_directory(self) -> None:
        base = f"/opt/klukai-ci/artifacts/{REV}/9-1/release"
        agent = FakeAgent(
            {
                f"{base}/manifest.json": manifest(),
                f"{base}/core-image.tar": b"image-bytes",
            }
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                release_artifact, "verify", return_value={"tag": "ok"}
            ) as verify,
        ):
            result = relay.fetch_image(relay.Guest(agent), "9-1", REV, Path(tmp))
        self.assertEqual(result, {"tag": "ok"})
        self.assertEqual(verify.call_args.args[3], f"klukai-core:ci-{REV[:7]}")

    def test_manifest_for_another_revision_stops_before_the_image_transfer(
        self,
    ) -> None:
        base = f"/opt/klukai-ci/artifacts/{REV}/9-1/release"
        other = "5d9c3f054d9f1a6c56da091a7f9bd3fbffc1df27"
        agent = FakeAgent(
            {
                f"{base}/manifest.json": manifest(other),
                f"{base}/core-image.tar": b"image-bytes",
            }
        )
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            relay.fetch_image(relay.Guest(agent), "9-1", REV, Path(tmp))
        self.assertNotIn(f"{base}/core-image.tar", agent.reads)

    def test_run_identity_cannot_escape_the_artifact_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            relay.fetch_image(relay.Guest(FakeAgent({})), "../../etc", REV, Path(tmp))


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


class ReleaseManifestTests(unittest.TestCase):
    def test_manifest_must_match_the_revision_and_carry_layers(self) -> None:
        good = json.loads(manifest())
        release_artifact.check_manifest(good, REV)
        for broken in (
            {**good, "schema": 2},
            {**good, "layers": []},
            {**good, "tar_sha256": "x"},
        ):
            with self.assertRaises(ValueError):
                release_artifact.check_manifest(broken, REV)
        with self.assertRaises(ValueError):
            release_artifact.check_manifest(good, "short")


if __name__ == "__main__":
    unittest.main()
