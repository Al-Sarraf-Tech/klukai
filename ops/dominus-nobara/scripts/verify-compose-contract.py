#!/usr/bin/env python3
"""Fail closed when the rendered Dominus stack can escape its RAID contract."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any


EXPECTED_PROJECT = "dominus-ai-stack"
EXPECTED_TAILSCALE_IPV4 = "100.107.121.5"
EXPECTED_PUBLIC_PORTS = {1234, 8301, 8390}
EPHEMERAL_BINDS = {Path("/run/user/1000/dominus-gpu")}
MAX_MODEL_IDLE_TTL_SECONDS = 900
SPEACHES_TIMER_SAFETY_CUTOFF_SECONDS = 895


def validate_private_env_file(env_file: Path, *, expected_uid: int) -> list[str]:
    """Require the deployed Compose secret file to be private and non-linkable."""

    try:
        metadata = env_file.lstat()
    except OSError as error:
        return [f"stack env file is unavailable: {error}"]
    errors: list[str] = []
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        errors.append("stack env file must be a regular non-symlink file")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        errors.append("stack env file mode must be exactly 0600")
    if metadata.st_uid != expected_uid:
        errors.append(f"stack env file must be owned by uid {expected_uid}")
    return errors


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _gpu_devices(service: dict[str, Any]) -> list[object]:
    deploy = service.get("deploy")
    if not isinstance(deploy, dict):
        return []
    resources = deploy.get("resources")
    if not isinstance(resources, dict):
        return []
    reservations = resources.get("reservations")
    if not isinstance(reservations, dict):
        return []
    devices = reservations.get("devices")
    return devices if isinstance(devices, list) else []


def _direct_gpu_access(service: dict[str, Any]) -> list[str]:
    """Return non-deploy Compose declarations that can expose NVIDIA devices."""
    declarations: list[str] = []
    if service.get("gpus") not in (None, [], ""):
        declarations.append("gpus")
    if service.get("runtime") == "nvidia":
        declarations.append("runtime:nvidia")
    if service.get("device_requests") not in (None, [], ""):
        declarations.append("device_requests")
    devices = service.get("devices")
    if isinstance(devices, list) and any(
        "nvidia" in str(device).lower() for device in devices
    ):
        declarations.append("devices:/dev/nvidia*")
    return declarations


def _bind_for_target(
    service: dict[str, Any], target: str
) -> dict[str, Any] | None:
    volumes = service.get("volumes")
    if not isinstance(volumes, list):
        return None
    for volume in volumes:
        if isinstance(volume, dict) and volume.get("target") == target:
            return volume
    return None


def validate_document(
    document: object,
    *,
    raid_root: Path,
    source_root: Path,
) -> list[str]:
    """Return every contract violation in a rendered Compose document."""
    raid_root = raid_root.resolve(strict=False)
    source_root = source_root.resolve(strict=False)
    errors: list[str] = []
    if not isinstance(document, dict):
        return ["rendered Compose document is not an object"]
    if document.get("name") != EXPECTED_PROJECT:
        errors.append(f"Compose project must be {EXPECTED_PROJECT!r}")

    services = document.get("services")
    if not isinstance(services, dict):
        return errors + ["rendered Compose document has no service map"]

    public_ports: set[int] = set()
    for service_name, raw_service in services.items():
        if not isinstance(service_name, str) or not isinstance(raw_service, dict):
            errors.append("rendered Compose service entry is malformed")
            continue
        service: dict[str, Any] = raw_service

        build = service.get("build")
        if isinstance(build, dict) and isinstance(build.get("context"), str):
            context = Path(build["context"])
            if not context.is_absolute() or not _inside(context, source_root):
                errors.append(
                    f"{service_name}: build context escapes the canonical RAID source tree: {context}"
                )

        volumes = service.get("volumes", [])
        if not isinstance(volumes, list):
            errors.append(f"{service_name}: volumes is not a list")
            continue
        for volume in volumes:
            if not isinstance(volume, dict):
                errors.append(f"{service_name}: volume entry is not long syntax")
                continue
            if volume.get("type") != "bind":
                errors.append(
                    f"{service_name}: persistent volume type must be an explicit bind"
                )
                continue
            source_value = volume.get("source")
            target_value = volume.get("target", "<unknown>")
            if not isinstance(source_value, str):
                errors.append(f"{service_name}:{target_value}: bind source is absent")
                continue
            source = Path(source_value)
            if not source.is_absolute():
                errors.append(f"{service_name}:{target_value}: bind source is not absolute")
            if not (
                _inside(source.resolve(strict=False), raid_root)
                or _inside(source.resolve(strict=False), source_root)
                or source in EPHEMERAL_BINDS
            ):
                errors.append(
                    f"{service_name}:{target_value}: bind source escapes RAID/runtime allowlist: {source}"
                )
            bind_options = volume.get("bind")
            if not isinstance(bind_options, dict) or bind_options.get(
                "create_host_path"
            ) is not False:
                errors.append(
                    f"{service_name}:{target_value}: create_host_path must be false"
                )

        ports = service.get("ports", [])
        if ports is None:
            ports = []
        if not isinstance(ports, list):
            errors.append(f"{service_name}: ports is not a list")
            continue
        for port in ports:
            if not isinstance(port, dict):
                errors.append(f"{service_name}: published port is not rendered long syntax")
                continue
            if port.get("host_ip") != EXPECTED_TAILSCALE_IPV4:
                errors.append(
                    f"{service_name}: published port is not bound to {EXPECTED_TAILSCALE_IPV4}"
                )
            try:
                public_ports.add(int(port["published"]))
            except (KeyError, TypeError, ValueError):
                errors.append(f"{service_name}: published port is missing or invalid")

    if public_ports != EXPECTED_PUBLIC_PORTS:
        errors.append(
            "published ports must be exactly "
            f"{sorted(EXPECTED_PUBLIC_PORTS)}, got {sorted(public_ports)}"
        )

    comfyui = services.get("comfyui")
    if not isinstance(comfyui, dict) or comfyui.get("ports"):
        errors.append("ComfyUI must have no raw host port; use the authenticated gateway facade")

    transcription = services.get("transcriptionsuite")
    if not isinstance(transcription, dict):
        errors.append("TranscriptionSuite service is absent")
    else:
        if transcription.get("entrypoint") != [
            "/bootstrap/offline-entrypoint.sh"
        ]:
            errors.append("TranscriptionSuite must use its hard-disable wrapper")
        if transcription.get("ports"):
            errors.append(
                "TranscriptionSuite must have no host port until inbound auth is proven"
            )
        exposed = transcription.get("expose")
        if exposed not in (["9786"], ["9786/tcp"]):
            errors.append("TranscriptionSuite may expose only internal port 9786")
        transcription_environment = transcription.get("environment")
        if not isinstance(transcription_environment, dict):
            errors.append("TranscriptionSuite environment is absent")
        else:
            if transcription_environment.get("TLS_ENABLED") != "true":
                errors.append("TranscriptionSuite TLS must be literal true")
            if (
                transcription_environment.get(
                    "DOMINUS_TRANSCRIPTION_PRODUCTION_ENABLED"
                )
                != "false"
            ):
                errors.append(
                    "TranscriptionSuite production API must remain hard-disabled"
                )
            if (
                "CUDA_VISIBLE_DEVICES" in transcription_environment
                or "NVIDIA_VISIBLE_DEVICES" in transcription_environment
            ):
                errors.append(
                    "disabled TranscriptionSuite must not receive GPU visibility"
                )
        if _gpu_devices(transcription) or _direct_gpu_access(transcription):
            errors.append(
                "disabled TranscriptionSuite must not receive direct NVIDIA access"
            )

    transcription_bootstrap = services.get("transcriptionsuite-bootstrap")
    if not isinstance(transcription_bootstrap, dict):
        errors.append("TranscriptionSuite bootstrap service is absent")
    else:
        if transcription_bootstrap.get("entrypoint") != [
            "/bootstrap/bootstrap.sh"
        ]:
            errors.append("TranscriptionSuite bootstrap must use its hard-disable wrapper")
        bootstrap_environment = transcription_bootstrap.get("environment")
        if not isinstance(bootstrap_environment, dict):
            errors.append("TranscriptionSuite bootstrap environment is absent")
        else:
            if (
                bootstrap_environment.get(
                    "DOMINUS_TRANSCRIPTION_BOOTSTRAP_ENABLED"
                )
                != "false"
            ):
                errors.append("TranscriptionSuite bootstrap must remain hard-disabled")
            if (
                "CUDA_VISIBLE_DEVICES" in bootstrap_environment
                or "NVIDIA_VISIBLE_DEVICES" in bootstrap_environment
            ):
                errors.append(
                    "disabled TranscriptionSuite bootstrap must not receive GPU visibility"
                )
        if _gpu_devices(transcription_bootstrap) or _direct_gpu_access(
            transcription_bootstrap
        ):
            errors.append(
                "disabled TranscriptionSuite bootstrap must not receive direct NVIDIA access"
            )

    gateway = services.get("lmstudio-compat")
    voice = services.get("companion-voice")
    if not isinstance(gateway, dict) or not isinstance(voice, dict):
        errors.append("gateway and companion voice services are required")
    else:
        gateway_environment = gateway.get("environment")
        voice_environment = voice.get("environment")
        if not isinstance(gateway_environment, dict) or not isinstance(
            voice_environment, dict
        ):
            errors.append("gateway/voice lease environments are absent")
        else:
            if gateway_environment.get("COMPANION_VOICE_URL") != (
                "http://companion-voice:8301"
            ):
                errors.append("gateway voice cleanup must use the internal service URL")
            if gateway_environment.get("GPU_LEASE_MARKER") != (
                "/run/dominus-gpu/non-llm-lease.json"
            ) or voice_environment.get("GPU_LEASE_MARKER") != (
                "/run/dominus-gpu/non-llm-lease.json"
            ):
                errors.append("gateway and voice must share the canonical lease marker")
            gateway_voice_token = gateway_environment.get(
                "COMPANION_VOICE_BEARER_TOKEN"
            )
            voice_token = voice_environment.get("VOICE_API_TOKEN")
            if (
                not isinstance(gateway_voice_token, str)
                or len(gateway_voice_token) < 32
                or gateway_voice_token != voice_token
            ):
                errors.append("gateway cleanup and voice API tokens must match securely")
        gateway_runtime = _bind_for_target(gateway, "/run/dominus-gpu")
        voice_runtime = _bind_for_target(voice, "/run/dominus-gpu")
        if gateway.get("user") != "1000:1001":
            errors.append("gateway must own lease markers as uid 1000/gid 1001")
        if not isinstance(gateway_runtime, dict) or gateway_runtime.get("read_only") is True:
            errors.append("gateway GPU runtime bind must be writable")
        if not isinstance(voice_runtime, dict) or voice_runtime.get("read_only") is not True:
            errors.append("voice GPU runtime bind must be read-only")
        group_add = voice.get("group_add")
        if not isinstance(group_add, list) or "1001" not in group_add:
            errors.append("voice must join gid 1001 to read mode-0640 lease markers")

    speaches = services.get("speaches")
    if not isinstance(speaches, dict):
        errors.append("Speaches service is absent")
    else:
        environment = speaches.get("environment")
        if not isinstance(environment, dict):
            errors.append("Speaches environment is absent")
        else:
            if environment.get("WHISPER__INFERENCE_DEVICE") != "cpu":
                errors.append("Speaches must remain CPU-only until complete GPU cleanup exists")
            if environment.get("WHISPER__COMPUTE_TYPE") != "int8":
                errors.append("CPU-only Speaches must use the locked int8 compute type")
            if "CUDA_VISIBLE_DEVICES" in environment or "NVIDIA_VISIBLE_DEVICES" in environment:
                errors.append("CPU-only Speaches must not receive CUDA/NVIDIA visibility variables")
            try:
                shared_ttl = int(environment["WHISPER__TTL"])
                tts_ttl = int(environment["DOMINUS_SPEACHES_TTS_TTL"])
            except (KeyError, TypeError, ValueError):
                errors.append("Speaches STT/TTS TTLs must be rendered integers")
            else:
                if (
                    not 1 <= shared_ttl <= SPEACHES_TIMER_SAFETY_CUTOFF_SECONDS
                    or not 1 <= tts_ttl <= SPEACHES_TIMER_SAFETY_CUTOFF_SECONDS
                ):
                    errors.append(
                        "Speaches STT/TTS TTLs must be between 1 and the "
                        "895-second timer-safety cutoff under the 900-second policy"
                    )
                if shared_ttl != tts_ttl:
                    errors.append("pinned Speaches 0.8.3 requires matching STT/TTS TTLs")
        if _gpu_devices(speaches):
            errors.append("Speaches must not receive an NVIDIA device")
        direct_gpu_access = _direct_gpu_access(speaches)
        if direct_gpu_access:
            errors.append(
                "Speaches must not declare direct NVIDIA access: "
                + ", ".join(direct_gpu_access)
            )

    networks = document.get("networks")
    if not isinstance(networks, dict):
        errors.append("rendered Compose document has no network map")
    else:
        internal = networks.get("ai-internal")
        if not isinstance(internal, dict) or internal.get("internal") is not True:
            errors.append("ai-internal network must be internal")

    return errors


def render_compose(compose_file: Path, env_file: Path) -> dict[str, Any]:
    process = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "--file",
            str(compose_file),
            "--profile",
            "*",
            "config",
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if process.returncode != 0:
        message = process.stderr.strip() or f"docker compose exited {process.returncode}"
        raise RuntimeError(message)
    document = json.loads(process.stdout)
    if not isinstance(document, dict):
        raise RuntimeError("docker compose returned a non-object document")
    return document


def _assert_runtime_layout(
    document: dict[str, Any], *, raid_root: Path, source_root: Path
) -> list[str]:
    errors: list[str] = []
    if not os.path.ismount(raid_root):
        return [f"required RAID is not a mountpoint: {raid_root}"]
    try:
        resolved_raid = raid_root.resolve(strict=True)
        resolved_source = source_root.resolve(strict=True)
    except OSError as error:
        return [f"cannot resolve canonical RAID/source tree: {error}"]
    if not _inside(resolved_source, resolved_raid):
        errors.append(f"canonical source tree is not on the RAID: {resolved_source}")

    services = document.get("services", {})
    if not isinstance(services, dict):
        return errors
    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        for volume in service.get("volumes", []):
            if not isinstance(volume, dict) or volume.get("type") != "bind":
                continue
            source_value = volume.get("source")
            if not isinstance(source_value, str):
                continue
            source = Path(source_value)
            if source in EPHEMERAL_BINDS:
                if source.is_symlink() or not source.is_dir():
                    errors.append(f"ephemeral GPU runtime bind is unsafe or absent: {source}")
                continue
            try:
                resolved = source.resolve(strict=True)
            except OSError as error:
                errors.append(f"{service_name}: bind source is absent: {source}: {error}")
                continue
            if not _inside(resolved, resolved_raid):
                errors.append(
                    f"{service_name}: resolved bind source is not on the RAID: {resolved}"
                )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--raid-root", type=Path, default=Path("/mnt/nvmer0"))
    parser.add_argument("--expected-env-uid", type=int, default=os.getuid())
    parser.add_argument(
        "--static", action="store_true", help="skip live mount/existence assertions"
    )
    arguments = parser.parse_args()
    private_env_errors = validate_private_env_file(
        arguments.env_file, expected_uid=arguments.expected_env_uid
    )
    if private_env_errors:
        print(
            json.dumps(
                {"status": "rejected", "errors": private_env_errors}, sort_keys=True
            )
        )
        return 1
    try:
        document = render_compose(arguments.compose_file, arguments.env_file)
        errors = validate_document(
            document,
            raid_root=arguments.raid_root,
            source_root=arguments.source_root,
        )
        if not arguments.static:
            errors.extend(
                _assert_runtime_layout(
                    document,
                    raid_root=arguments.raid_root,
                    source_root=arguments.source_root,
                )
            )
    except (OSError, RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError):
        # Docker's rendered document contains credentials. Never echo its
        # stderr, parse context, environment, or values on a failed render.
        print(
            json.dumps(
                {
                    "status": "unverifiable",
                    "error": "Compose contract could not be rendered safely",
                }
            )
        )
        return 2
    if errors:
        print(json.dumps({"status": "rejected", "errors": errors}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "project": EXPECTED_PROJECT,
                "published_ports": sorted(EXPECTED_PUBLIC_PORTS),
                "raw_comfyui_port": False,
                "speaches_gpu": False,
                "transcriptionsuite_host_port": False,
                "transcriptionsuite_production_enabled": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
