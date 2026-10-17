from __future__ import annotations

import http.server
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request


OPS_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = OPS_ROOT / "scripts/klukai-alert-bridge.py"
SERVICE = OPS_ROOT / "systemd/klukai-alert-bridge.service"
SPEC = importlib.util.spec_from_file_location("klukai_alert_bridge", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


def webhook(*alerts: dict, status: str = "firing") -> dict:
    return {"version": "4", "status": status, "alerts": list(alerts)}


BURN = {
    "status": "firing",
    "labels": {
        "alertname": "KlukaiSLOBudgetBurn",
        "severity": "page",
        "method": "GET",
        "route": "/health",
    },
    "annotations": {
        "summary": "GET /health burn_rate objective exceeded",
        "runbook_url": "https://github.com/Al-Sarraf-Tech/klukai/blob/main/docs/runbooks/dependency-outage.md#availability",
    },
}


class NoteTests(unittest.TestCase):
    def test_note_names_alert_labels_summary_and_runbook(self) -> None:
        text = bridge.note(webhook(BURN))
        self.assertTrue(text.startswith("KLUKAI_ALERT FIRING: KlukaiSLOBudgetBurn"))
        self.assertIn("severity=page method=GET route=/health", text)
        self.assertIn("burn_rate objective exceeded", text)
        self.assertIn("runbook https://github.com/", text)

    def test_note_is_bounded_and_strips_control_characters(self) -> None:
        noisy = dict(BURN, annotations={"summary": "line\nbreak\x1b[31m" + "x" * 5000})
        text = bridge.note(webhook(*[noisy] * 9, status="resolved"))
        self.assertLessEqual(len(text), bridge.MAX_NOTE)
        self.assertNotIn("\n", text)
        self.assertNotIn("\x1b", text)
        self.assertTrue(text.startswith("KLUKAI_ALERT RESOLVED"))

    def test_dependency_label_is_named(self) -> None:
        circuit = {
            "labels": {
                "alertname": "KlukaiDependencyCircuitOpen",
                "severity": "ticket",
                "dep": "postgres",
            },
            "annotations": {"summary": "postgres breaker open"},
        }
        self.assertIn("(severity=ticket dep=postgres)", bridge.note(webhook(circuit)))

    def test_more_than_five_alerts_are_summarized(self) -> None:
        small = {"labels": {"alertname": "KlukaiCoreDown"}, "annotations": {}}
        self.assertIn("+2 more", bridge.note(webhook(*[small] * 7)))

    def test_non_alertmanager_payloads_are_rejected(self) -> None:
        for payload in ({}, {"version": "3", "alerts": [BURN]}, webhook()):
            with self.assertRaises(ValueError):
                bridge.note(payload)


class TokenTests(unittest.TestCase):
    def test_token_file_must_be_private_regular_and_nonempty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token"
            path.write_text("s3cret-for-test\n")
            path.chmod(0o600)
            self.assertEqual(bridge.read_token(str(path)), "s3cret-for-test")
            path.chmod(0o640)
            with self.assertRaises(ValueError):
                bridge.read_token(str(path))
            path.chmod(0o600)
            link = Path(tmp) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                bridge.read_token(str(link))
            path.write_text("")
            with self.assertRaises(ValueError):
                bridge.read_token(str(path))


class HttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.calls = root / "calls.jsonl"
        self.bus = root / "bus"
        self.bus.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"open({str(self.calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "sys.exit(int(os.environ.get('FAKE_BUS_EXIT', '0')))\n"
        )
        self.bus.chmod(self.bus.stat().st_mode | stat.S_IXUSR)
        handler = bridge.handler("tok", str(self.bus), "owner@amarillo", "klukai")
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        os.environ.pop("FAKE_BUS_EXIT", None)
        self.tmp.cleanup()

    def post(self, path: str, body: bytes, token: str | None = "tok") -> int:
        request = urllib.request.Request(self.url + path, data=body, method="POST")
        if token is not None:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as error:
            error.close()
            return error.code

    def test_authenticated_webhook_becomes_one_owner_note(self) -> None:
        self.assertEqual(self.post("/alert", json.dumps(webhook(BURN)).encode()), 200)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:3], ["send", "owner@amarillo", "--note"])
        self.assertTrue(calls[0][3].startswith("KLUKAI_ALERT FIRING"))
        self.assertEqual(calls[0][4:], ["--project", "klukai"])

    def test_bus_failure_asks_alertmanager_to_retry(self) -> None:
        os.environ["FAKE_BUS_EXIT"] = "1"
        self.assertEqual(self.post("/alert", json.dumps(webhook(BURN)).encode()), 502)

    def test_unauthenticated_malformed_and_oversized_requests_send_nothing(
        self,
    ) -> None:
        body = json.dumps(webhook(BURN)).encode()
        self.assertEqual(self.post("/alert", body, token=None), 401)
        self.assertEqual(self.post("/alert", body, token="wrong"), 401)
        self.assertEqual(self.post("/other", body), 404)
        self.assertEqual(self.post("/alert", b"not json"), 400)
        self.assertEqual(self.post("/alert", b"x" * (bridge.MAX_BODY + 1)), 413)
        self.assertFalse(self.calls.exists())


class DeploymentTests(unittest.TestCase):
    def test_refuses_to_listen_on_all_interfaces(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--bind",
                "0.0.0.0:19095",
                "--token-file",
                "/x",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gateway", result.stderr)

    def test_user_unit_binds_the_obs_gateway_with_a_private_token(self) -> None:
        unit = SERVICE.read_text()
        self.assertIn("--bind 192.168.128.1:19095", unit)
        self.assertIn("alert-bridge-token", unit)
        self.assertIn("UMask=0077", unit)
        self.assertIn("Restart=on-failure", unit)


if __name__ == "__main__":
    unittest.main()
