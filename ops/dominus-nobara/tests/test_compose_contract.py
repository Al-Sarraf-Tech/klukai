from __future__ import annotations

import copy
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


OPS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = OPS_ROOT.parents[1]
SCRIPT = OPS_ROOT / "scripts/verify-compose-contract.py"
SYSTEMD_UNIT = OPS_ROOT / "systemd/dominus-ai-stack.service"
SPEC = importlib.util.spec_from_file_location("verify_compose_contract", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
contract = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = contract
SPEC.loader.exec_module(contract)


class ComposeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        prior = {
            name: os.environ.get(name)
            for name in ("LM_STUDIO_TOKEN", "SPEACHES_API_KEY", "VOICE_API_TOKEN")
        }
        try:
            os.environ.update(
                {
                    "LM_STUDIO_TOKEN": "a" * 32,
                    "SPEACHES_API_KEY": "b" * 32,
                    "VOICE_API_TOKEN": "c" * 32,
                }
            )
            cls.document = contract.render_compose(
                OPS_ROOT / "compose.yaml", OPS_ROOT / "config/stack.env.example"
            )
        finally:
            for name, value in prior.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def errors(self, document: object) -> list[str]:
        return contract.validate_document(
            document,
            raid_root=Path("/mnt/nvmer0"),
            source_root=REPO_ROOT,
        )

    def test_real_rendered_compose_obeys_static_contract(self) -> None:
        self.assertEqual(self.errors(self.document), [])

    def test_start_reload_and_preflight_refuse_active_game_marker(self) -> None:
        unit = SYSTEMD_UNIT.read_text(encoding="utf-8")
        guard = "/usr/bin/test ! -e /run/user/1000/dominus-gpu/game-active"
        self.assertIn(f"ExecStartPre={guard}", unit)
        self.assertIn(f"ExecReload={guard}", unit)
        self.assertLess(
            unit.index(f"ExecReload={guard}"),
            unit.index("ExecReload=/usr/bin/docker compose"),
        )
        preflight = self.document["services"]["stack-preflight"]
        runtime = next(
            volume
            for volume in preflight["volumes"]
            if volume["target"] == "/run/dominus-gpu"
        )
        self.assertTrue(runtime["read_only"])
        command = " ".join(preflight["command"])
        self.assertIn("game marker is active", command)

    def test_compose_parser_rejects_duplicate_mapping_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compose_file = root / "compose.yaml"
            env_file = root / "stack.env"
            compose_file.write_text(
                "name: duplicate-test\n"
                "services:\n"
                "  example:\n"
                "    image: busybox:1\n"
                "    init: true\n"
                "    init: false\n",
                encoding="utf-8",
            )
            env_file.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "(?i)(mapping key|duplicate)"):
                contract.render_compose(compose_file, env_file)

    def test_root_disk_bind_and_implicit_creation_are_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        volume = document["services"]["comfyui"]["volumes"][0]
        volume["source"] = "/var/lib/dominus-models"
        volume["bind"]["create_host_path"] = True
        errors = self.errors(document)
        self.assertTrue(any("escapes RAID/runtime allowlist" in item for item in errors))
        self.assertTrue(any("create_host_path must be false" in item for item in errors))

    def test_stack_env_requires_regular_owner_only_mode_and_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / "stack.env"
            env_file.write_text("TOKEN=redacted\n", encoding="utf-8")
            env_file.chmod(0o600)
            self.assertEqual(
                contract.validate_private_env_file(
                    env_file, expected_uid=env_file.stat().st_uid
                ),
                [],
            )

            env_file.chmod(0o640)
            errors = contract.validate_private_env_file(
                env_file, expected_uid=env_file.stat().st_uid
            )
            self.assertTrue(any("exactly 0600" in item for item in errors))

            env_file.chmod(0o600)
            errors = contract.validate_private_env_file(
                env_file, expected_uid=env_file.stat().st_uid + 1
            )
            self.assertTrue(any("owned by uid" in item for item in errors))

            link = root / "stack-link.env"
            link.symlink_to(env_file)
            errors = contract.validate_private_env_file(
                link, expected_uid=env_file.stat().st_uid
            )
            self.assertTrue(any("regular non-symlink" in item for item in errors))

    def test_raw_comfy_port_is_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        document["services"]["comfyui"]["ports"] = [
            {
                "host_ip": "100.107.121.5",
                "target": 8188,
                "published": "8388",
                "protocol": "tcp",
            }
        ]
        errors = self.errors(document)
        self.assertTrue(any("ComfyUI must have no raw host port" in item for item in errors))
        self.assertTrue(any("published ports must be exactly" in item for item in errors))

    def test_transcriptionsuite_host_port_or_tls_downgrade_is_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        transcription = document["services"]["transcriptionsuite"]
        transcription["ports"] = [
            {
                "host_ip": "100.107.121.5",
                "target": 9786,
                "published": "9786",
                "protocol": "tcp",
            }
        ]
        transcription["environment"]["TLS_ENABLED"] = "false"
        transcription["environment"][
            "DOMINUS_TRANSCRIPTION_PRODUCTION_ENABLED"
        ] = "true"
        errors = self.errors(document)
        self.assertTrue(any("must have no host port" in item for item in errors))
        self.assertTrue(any("TLS must be literal true" in item for item in errors))
        self.assertTrue(any("must remain hard-disabled" in item for item in errors))

    def test_transcriptionsuite_is_internal_only_and_hard_disabled(self) -> None:
        transcription = self.document["services"]["transcriptionsuite"]
        self.assertFalse(transcription.get("ports"))
        self.assertIn(transcription.get("expose"), (["9786"], ["9786/tcp"]))
        self.assertEqual(transcription["environment"]["TLS_ENABLED"], "true")
        self.assertEqual(
            transcription["environment"][
                "DOMINUS_TRANSCRIPTION_PRODUCTION_ENABLED"
            ],
            "false",
        )
        bootstrap = self.document["services"]["transcriptionsuite-bootstrap"]
        self.assertEqual(
            bootstrap["environment"]["DOMINUS_TRANSCRIPTION_BOOTSTRAP_ENABLED"],
            "false",
        )
        for service in (transcription, bootstrap):
            self.assertFalse(contract._gpu_devices(service))
            self.assertFalse(contract._direct_gpu_access(service))

    def test_disabled_transcription_bootstrap_gpu_bypass_is_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        bootstrap = document["services"]["transcriptionsuite-bootstrap"]
        bootstrap["environment"]["DOMINUS_TRANSCRIPTION_BOOTSTRAP_ENABLED"] = (
            "true"
        )
        bootstrap["environment"]["CUDA_VISIBLE_DEVICES"] = "0"
        bootstrap["entrypoint"] = ["/app/docker/docker-entrypoint.sh"]
        bootstrap["deploy"] = {
            "resources": {"reservations": {"devices": [{"driver": "nvidia"}]}}
        }
        errors = self.errors(document)
        self.assertTrue(any("bootstrap must remain hard-disabled" in item for item in errors))
        self.assertTrue(any("must not receive GPU visibility" in item for item in errors))
        self.assertTrue(any("direct NVIDIA access" in item for item in errors))
        self.assertTrue(any("hard-disable wrapper" in item for item in errors))

    def test_disabled_transcription_wrappers_exit_before_upstream(self) -> None:
        for script in (
            OPS_ROOT / "config/transcriptionsuite/offline-entrypoint.sh",
            OPS_ROOT / "config/transcriptionsuite/bootstrap.sh",
        ):
            with self.subTest(script=script.name):
                process = subprocess.run(
                    [str(script)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env={"PATH": os.environ["PATH"]},
                )
                self.assertEqual(process.returncode, 78)
                self.assertIn("disabled pending all admission gates", process.stderr)

    def test_gpu_speaches_is_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        document["services"]["speaches"]["environment"][
            "WHISPER__INFERENCE_DEVICE"
        ] = "cuda"
        document["services"]["speaches"]["environment"]["CUDA_VISIBLE_DEVICES"] = "0"
        document["services"]["speaches"]["deploy"] = {
            "resources": {"reservations": {"devices": [{"driver": "nvidia"}]}}
        }
        document["services"]["speaches"]["gpus"] = "all"
        document["services"]["speaches"]["runtime"] = "nvidia"
        document["services"]["speaches"]["devices"] = ["/dev/nvidia0:/dev/nvidia0"]
        errors = self.errors(document)
        self.assertTrue(any("Speaches must remain CPU-only" in item for item in errors))
        self.assertTrue(any("visibility variables" in item for item in errors))
        self.assertTrue(any("must not receive an NVIDIA device" in item for item in errors))
        self.assertTrue(any("must not declare direct NVIDIA access" in item for item in errors))

    def test_speaches_stt_tts_ttl_mismatch_is_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        document["services"]["speaches"]["environment"]["WHISPER__TTL"] = "600"
        document["services"]["speaches"]["environment"][
            "DOMINUS_SPEACHES_TTS_TTL"
        ] = "896"
        errors = self.errors(document)
        self.assertTrue(any("895-second timer-safety cutoff" in item for item in errors))
        self.assertTrue(any("matching STT/TTS TTLs" in item for item in errors))

    def test_public_port_must_use_literal_tailscale_address(self) -> None:
        document = copy.deepcopy(self.document)
        document["services"]["lmstudio-compat"]["ports"][0]["host_ip"] = "0.0.0.0"
        errors = self.errors(document)
        self.assertTrue(any("not bound to 100.107.121.5" in item for item in errors))

    def test_voice_cannot_bypass_read_only_shared_lease_marker(self) -> None:
        document = copy.deepcopy(self.document)
        voice = document["services"]["companion-voice"]
        voice["environment"]["GPU_LEASE_MARKER"] = "/tmp/optional-lease.json"
        voice["group_add"] = []
        runtime = next(
            volume
            for volume in voice["volumes"]
            if volume["target"] == "/run/dominus-gpu"
        )
        runtime["read_only"] = False
        errors = self.errors(document)
        self.assertTrue(any("share the canonical lease marker" in item for item in errors))
        self.assertTrue(any("runtime bind must be read-only" in item for item in errors))
        self.assertTrue(any("join gid 1001" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
