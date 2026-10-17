"""Run explicitly bound baseline/candidate images on disposable real backends."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import platform
import secrets
import signal
from typing import Any

import httpx

from .perf_gate import compare, image_reference, revision
from .perf_measure import Sampler
from .perf_network import LoopbackProxy
from .perf_resources import (
    RESOURCES,
    DisposableStack,
    docker,
    inspect_image,
)
from .perf_schema import MIN_ROUNDS, MIN_SAMPLES, digest
from .perf_seed import SEED_VERSION, create_database, redis_client, seed
from . import core_source
from .perf_recording import ACTIVE, Recorder
from .perf_evidence_identity import helpers

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "docker/core/app/observability/slos.json"


def write_exclusive(path: Path, value: object) -> None:
    with path.open("x") as output:
        json.dump(value, output, indent=2, allow_nan=False)
        output.write("\n")


def environment(stack: DisposableStack, database: str) -> dict[str, str]:
    """Explicit fresh test settings; never copy host provider/auth credentials."""
    fixture = "http://fixture:8088"
    return {
        "DATABASE_URL": f"postgresql://perf_test:{stack.password}@postgres:5432/{database}",
        "REDIS_URL": "redis://redis:6379/1",
        "QDRANT_URL": "http://qdrant:6333",
        "PERSONALITY_PATH": "/perf-fixtures/personality.yaml",
        "SEED_PASSWORD_JALSARRAF": stack.password,
        "SEED_PASSWORD_RICKY": stack.password,
        "LM_STUDIO_URL": fixture,
        "LM_STUDIO_TOKEN": stack.password,
        "MCP_URL": fixture,
        "DATA_URL": fixture,
        "INFERENCE_URL": fixture,
        "VOICE_URL": fixture,
        "VOICE_STT_URL": fixture,
        "VOICE_BACKEND": "dominus",
        "VOICE_ASYNC_JOBS": "true",
        "VOICE_API_TOKEN": stack.password,
        "COMFYUI_URL": fixture,
        "KLUKAI_CLAUDE_PRIMARY": "false",
        "KLUKAI_BRAVE_ENABLED": "false",
        "KLUKAI_CLAUDE_CREATIVE_ENABLED": "false",
        "KLUKAI_IMAGE_PROVIDER": "agy",
        "KLUKAI_ACCOUNT_IMAGE_WORKER_URL": fixture,
        "KLUKAI_CLOUD_IMAGE_DAILY_LIMIT": "0",
        "KLUKAI_CLOUD_IMAGE_MONTHLY_LIMIT": "0",
        "HER_POV_EXECUTION": "inline",
        "AUDIO_DIR": "/audio",
        "IMAGES_DIR": "/images",
        "OTEL_SDK_DISABLED": "true",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def provenance(stack: DisposableStack, policy: dict[str, Any]) -> dict[str, str]:
    sources = {
        file.name: file.read_text()
        for file in sorted((ROOT / "tools/ci").glob("perf_*.py"))
    }
    write_exclusive(
        stack.output / "harness-sources.json",
        {"files": sources, "sha256": digest(sources)},
    )
    docker_version = json.loads(docker("version", "--format", "{{json .}}"))
    host = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "docker": docker("version", "--format", "{{json .}}"),
        "cpu": Path("/proc/cpuinfo").read_text(),
    }
    write_exclusive(
        stack.output / "inputs.json",
        {
            "python": platform.python_version(),
            "httpx": httpx.__version__,
            "docker": docker_version,
            "host": host,
            "backends": stack.backends,
            "backend_images": stack.backend_images,
            "backend_inputs": stack.backend_inputs,
            "resources": RESOURCES,
            "policy": policy,
            "personality_sha256": digest(
                (ROOT / "config/personality.yaml").read_text()
            ),
        },
    )
    return {
        "harness": digest(sources),
        "fixture": digest(
            {
                "script": sources["perf_fixtures.py"],
                "personality": (ROOT / "config/personality.yaml").read_text(),
                "backends": stack.backend_images,
            }
        ),
        "seed": digest(SEED_VERSION),
        "policy": digest(policy),
        "host": digest(host),
    }


async def run_round(
    stack: DisposableStack,
    image: dict[str, Any],
    role: str,
    index: int,
    config: dict[str, Any],
) -> dict[str, Any]:
    stack.check_temperature()
    database = f"perf_{role}_{index}"
    await create_database(stack, database)
    cache = redis_client(stack)
    await cache.flushdb()
    name = stack._create(
        "core",
        image["id"],
        env=environment(stack, database),
        suffix=f"{role}-{index}",
        fixture_volume=True,
    )
    actual = stack.inspect_container(name, "core")
    if actual["image"] != image["id"]:
        raise RuntimeError("Actual running core image does not match declared input")
    proxy = LoopbackProxy(actual["ip"], 8300)
    await proxy.start()
    started = datetime.now(UTC).isoformat()
    try:
        async with httpx.AsyncClient(
            base_url=proxy.url, timeout=20, trust_env=False, follow_redirects=False
        ) as client:
            sampler = Sampler(client, cache, stack.password, stack.check_temperature)
            await sampler.initialize()
            await seed(stack, database)
            routes = await sampler.measure(
                config["warmup"], config["samples"], config["concurrency"]
            )
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            response = await client.get(
                f"http://{stack.backends['fixture']['ip']}:8088/fixture-stats"
            )
            response.raise_for_status()
            fixture = response.json()
            if fixture.get("external_calls") != 0 or fixture.get("gpu_calls") != 0:
                raise RuntimeError("Fixture isolation evidence invalid")
        return {
            "round": index,
            "started_at": started,
            "finished_at": datetime.now(UTC).isoformat(),
            "actual_container": actual,
            "fixture": fixture,
            "routes": routes,
        }
    finally:
        await cache.aclose()
        await proxy.close()
        stack.stop_core(name)


async def _execute(args: argparse.Namespace) -> dict[str, Any]:
    images = {}
    for role in ("baseline", "candidate"):
        reference, source = (
            getattr(args, role + "_image"),
            getattr(args, role + "_revision"),
        )
        image_reference(reference)
        revision(source)
        info = inspect_image(reference)
        verified = info["revision"] == source
        if not verified and not args.development:
            raise ValueError(
                f"{role} immutable image does not attest requested source revision"
            )
        images[role] = {
            **info,
            "input": reference,
            "declared_revision": source,
            "image_revision": info["revision"],
            "verified": verified,
        }
    if not args.development and (
        args.baseline_revision == args.candidate_revision
        or images["baseline"]["id"] == images["candidate"]["id"]
    ):
        raise ValueError("Certifying performance needs a distinct previous baseline")
    if (
        args.samples < MIN_SAMPLES
        or args.rounds < MIN_ROUNDS
        or args.warmup < 1
        or args.concurrency != 1
    ):
        raise ValueError(
            "Need at least100 samples,3 rounds,positive warmup,concurrency1"
        )
    policy = json.loads(POLICY.read_text())
    config = {
        "samples": args.samples,
        "rounds": args.rounds,
        "warmup": args.warmup,
        "concurrency": args.concurrency,
        "seed_version": 1,
    }
    stack = DisposableStack(args.out, ROOT)
    started = datetime.now(UTC).isoformat()
    run_id = secrets.token_hex(16)
    reports: dict[str, Any] = {}
    try:
        stack.start(certifying=not args.development)
        fingerprints = provenance(stack, policy)
        for role in ("baseline", "candidate"):
            reports[role] = {
                "schema": 1,
                "role": role,
                "run_id": run_id,
                "image": images[role],
                "started_at": started,
                "provenance": fingerprints,
                "configuration": config,
                "resources": RESOURCES,
                "development_only": args.development,
                "rounds": [],
            }
        for index in range(args.rounds):
            order = (
                ("baseline", "candidate")
                if index % 2 == 0
                else ("candidate", "baseline")
            )
            for role in order:
                print(f"round={index} role={role} started", flush=True)
                row = await run_round(stack, images[role], role, index, config)
                reports[role]["rounds"].append(row)
                write_exclusive(args.out / f"{role}-round-{index}.json", row)
                print(f"round={index} role={role} measured", flush=True)
    finally:
        try:
            stack.close()
        finally:
            for role, report in reports.items():
                report.update(
                    finished_at=datetime.now(UTC).isoformat(),
                    cleanup_verified=stack.cleanup_verified,
                    cpu_temperature_max_c=stack.maximum_temperature,
                    thermal_telemetry=stack.thermal_telemetry,
                )
                write_exclusive(args.out / f"{role}.json", report)
    result = compare(
        reports["baseline"],
        reports["candidate"],
        policy=policy,
        baseline_image=args.baseline_image,
        candidate_image=args.candidate_image,
        baseline_revision=args.baseline_revision,
        candidate_revision=args.candidate_revision,
        allow_development=args.development,
    )
    write_exclusive(args.out / "gate.json", result)
    return result


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    """Bind clean committed source before creating any benchmark target."""
    root = getattr(args, "root", ROOT)
    if args.out.exists():
        raise FileExistsError("Never refresh an existing performance output")
    core_source.require(
        root == ROOT, "Producer must execute the selected repository helpers"
    )
    core_source.reserve_output(root, args.out)
    captured = core_source.snapshot(
        root, args.out, args.candidate_revision, development=args.development
    )
    if not args.development:
        revision(args.baseline_revision)
        core_source.require(
            args.baseline_revision != args.candidate_revision,
            "Distinct prior source is required",
        )
        core_source.git(root, "cat-file", "-e", args.baseline_revision + "^{commit}")
        core_source.git(
            root,
            "merge-base",
            "--is-ancestor",
            args.baseline_revision,
            args.candidate_revision,
        )
    producer = helpers(root)
    recorder = Recorder(args.out)
    token = ACTIVE.set(recorder)
    started = datetime.now(UTC).isoformat()
    try:
        result = await _execute(args)
        core_source.unchanged(root, args.out, captured)
        if helpers(root) != producer:
            raise ValueError("Executing performance helpers changed during execution")
        raw = {
            name: value
            for name, value in core_source.inventory(args.out).items()
            if not name.startswith("source/") and name != "source.tar"
        }
        write_exclusive(
            args.out / "manifest.json",
            {
                "schema_version": 1,
                "source_revision": args.candidate_revision,
                "source": captured,
                "producer_sha256": producer,
                "raw_sha256": raw,
                "started_at": started,
                "finished_at": datetime.now(UTC).isoformat(),
                "passed": result["passed"],
                "certifying": result["certifying"],
                "development": args.development,
                "source_unchanged": True,
            },
        )
        if not args.development:
            from .perf_evidence import validate

            validate(root, args.out, args.candidate_revision, 1)
        return result
    finally:
        ACTIVE.reset(token)


async def run_with_signals(args: argparse.Namespace) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("No active performance task")
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        return await execute(args)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for field in (
        "baseline-image",
        "candidate-image",
        "baseline-revision",
        "candidate-revision",
    ):
        parser.add_argument("--" + field, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--concurrency", type=int, choices=[1], default=1)
    parser.add_argument(
        "--development",
        action="store_true",
        help="Collect noncertifying harness proof; cannot pass the standalone CI gate",
    )
    args = parser.parse_args()
    try:
        result = asyncio.run(run_with_signals(args))
    except (Exception, KeyboardInterrupt, asyncio.CancelledError) as error:
        # Exception messages from dependencies may contain fresh disposable secrets.
        if args.out.is_dir():
            write_exclusive(
                args.out / "failure.json",
                {
                    "type": type(error).__name__,
                    "message": "Harness failed; inspect private disposable container logs",
                    "certifying": False,
                },
            )
        # Disposable credentials die with their containers; the reason is needed
        # to diagnose a refused CI gate.
        detail = str(error).replace("\n", " ")[:400]
        parser.exit(
            1, f"Performance stack failed closed: {type(error).__name__}: {detail}\n"
        )
    print(
        json.dumps(
            {key: result[key] for key in ("passed", "certifying", "max_p99_ratio")}
        )
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
