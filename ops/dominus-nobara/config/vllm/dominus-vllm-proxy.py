#!/usr/bin/env python3
"""Fail-closed lazy TCP proxy for the preserved native vLLM service.

The proxy stays reachable during a game so clients receive a prompt JSON 503.
It never refreshes activity merely because an idle TCP connection is open:
only connection acceptance, bytes actually moved, and connection close renew
the deadline. A local exact timer and a separate watchdog both enforce the
same non-configurable 900-second maximum idle residency.
"""

from __future__ import annotations

import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import time

from dominus_gpu_lease import LeaseStateError, active_lease


LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8000
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8001
BACKEND_UNIT = "vllm-server.service"

RAID_MOUNT = Path("/mnt/nvmer0")
STATE_ROOT = RAID_MOUNT / "services/ai-stack/state/vllm"
STATE_PATH = STATE_ROOT / "activity-state.json"
LOG_PATH = RAID_MOUNT / "services/ai-stack/logs/vllm/vllm-proxy.log"
GAME_MARKER = Path("/run/user/1000/dominus-gpu/game-active")
VRAM_PREFLIGHT = Path("/home/jalsarraf/.local/bin/dominus-vllm-vram-preflight")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")

# b10200 and this native backend share a user requirement: no LLM may remain
# resident beyond 900 idle seconds. The native backend is hard-killed at
# 895 seconds so scheduler latency and process teardown remain below
# that ceiling. These values have no environment or client override.
MAX_IDLE_TTL_SECONDS = 900
HARD_STOP_AFTER_NS = 895_000_000_000
STARTUP_TIMEOUT_SECONDS = 75
HEALTH_POLL_SECONDS = 0.5
CONNECT_TIMEOUT_SECONDS = 0.5

BOOT_ID = BOOT_ID_PATH.read_text(encoding="utf-8").strip()
log = logging.getLogger("dominus-vllm-proxy")

_start_stop_lock = asyncio.Lock()
_active_connections = 0
_generation = 0
_deadline_ns: int | None = None
_deadline_handle: asyncio.TimerHandle | None = None


class ServiceUnavailable(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def gpu_lease_block() -> ServiceUnavailable | None:
    try:
        lease = active_lease()
    except (OSError, LeaseStateError):
        return ServiceUnavailable(
            "gpu_lease_state_invalid",
            "The non-LLM GPU lease marker is invalid; native vLLM is fail-closed",
        )
    if lease is None:
        return None
    return ServiceUnavailable(
        "gpu_leased", f"The GPU is leased to the {lease.workload} workload"
    )


def configure_logging() -> None:
    if not os.path.ismount(RAID_MOUNT):
        raise RuntimeError(f"required RAID is not mounted: {RAID_MOUNT}")
    if not STATE_ROOT.is_dir() or not LOG_PATH.parent.is_dir():
        raise RuntimeError("vLLM RAID state/log directories were not initialized")
    handler = RotatingFileHandler(
        LOG_PATH, maxBytes=25 * 1024 * 1024, backupCount=4, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [dominus-vllm-proxy] %(levelname)s %(message)s")
    )
    log.setLevel(logging.INFO)
    log.addHandler(handler)


def _state_document(now_ns: int, deadline_ns: int | None) -> dict[str, object]:
    return {
        "version": 1,
        "boot_id": BOOT_ID,
        "generation": _generation,
        "last_activity_monotonic_ns": now_ns,
        "hard_stop_monotonic_ns": deadline_ns,
        "active_connections": _active_connections,
        "max_idle_ttl_seconds": MAX_IDLE_TTL_SECONDS,
    }


def _write_state(now_ns: int, deadline_ns: int | None) -> None:
    temporary = STATE_PATH.with_name(
        f".{STATE_PATH.name}.{os.getpid()}.{_generation}.partial"
    )
    payload = json.dumps(_state_document(now_ns, deadline_ns), sort_keys=True) + "\n"
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, STATE_PATH)


def _backend_reachable() -> bool:
    try:
        with socket.create_connection(
            (BACKEND_HOST, BACKEND_PORT), timeout=CONNECT_TIMEOUT_SECONDS
        ):
            return True
    except OSError:
        return False


async def _run_systemctl(*arguments: str, timeout: float = 10.0) -> int:
    process = await asyncio.create_subprocess_exec(
        "systemctl",
        "--user",
        *arguments,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 124


async def _backend_active() -> bool:
    return await _run_systemctl("is-active", "--quiet", BACKEND_UNIT, timeout=3) == 0


async def _hard_stop_backend(reason: str) -> None:
    """Make the process/cgroup empty; vLLM sleep mode is not sufficient."""
    log.warning("hard-stopping %s: %s", BACKEND_UNIT, reason)
    await _run_systemctl(
        "kill", "--kill-whom=all", "--signal=SIGKILL", BACKEND_UNIT, timeout=3
    )
    await _run_systemctl("stop", BACKEND_UNIT, timeout=8)


async def _enforce_local_deadline(generation: int, deadline_ns: int) -> None:
    global _deadline_ns
    async with _start_stop_lock:
        if generation != _generation or deadline_ns != _deadline_ns:
            return
        if time.monotonic_ns() < deadline_ns:
            _schedule_deadline(generation, deadline_ns)
            return
        await _hard_stop_backend("895-second idle safety deadline")
        _deadline_ns = None
        _write_state(time.monotonic_ns(), None)


def _schedule_deadline(generation: int, deadline_ns: int) -> None:
    global _deadline_handle
    if _deadline_handle is not None:
        _deadline_handle.cancel()
    delay = max(0.0, (deadline_ns - time.monotonic_ns()) / 1_000_000_000)
    loop = asyncio.get_running_loop()
    _deadline_handle = loop.call_later(
        delay,
        lambda: asyncio.create_task(_enforce_local_deadline(generation, deadline_ns)),
    )


def record_activity() -> None:
    """Atomically renew from real traffic; idle sockets do not call this."""
    global _deadline_ns, _generation
    now_ns = time.monotonic_ns()
    _generation += 1
    _deadline_ns = now_ns + HARD_STOP_AFTER_NS
    _write_state(now_ns, _deadline_ns)
    _schedule_deadline(_generation, _deadline_ns)


def restore_locked_deadline() -> int:
    """Restore only a current-boot, non-future, exactly locked deadline."""
    global _deadline_ns, _generation
    document = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("unsupported activity state")
    if document.get("boot_id") != BOOT_ID:
        raise ValueError("activity state belongs to another boot")
    if document.get("max_idle_ttl_seconds") != MAX_IDLE_TTL_SECONDS:
        raise ValueError("activity state does not contain the locked TTL")
    now_ns = time.monotonic_ns()
    last_activity = document.get("last_activity_monotonic_ns")
    deadline = document.get("hard_stop_monotonic_ns")
    if not isinstance(last_activity, int) or not isinstance(deadline, int):
        raise ValueError("activity state has no armed deadline")
    if last_activity > now_ns:
        raise ValueError("activity state claims a future monotonic timestamp")
    if deadline != last_activity + HARD_STOP_AFTER_NS:
        raise ValueError("activity state deadline is not the fixed 895-second boundary")
    generation = document.get("generation")
    if not isinstance(generation, int) or generation < 0:
        raise ValueError("activity state has an invalid generation")
    _generation = generation
    _deadline_ns = deadline
    _schedule_deadline(_generation, deadline)
    return deadline


async def _run_vram_preflight() -> bool:
    if not VRAM_PREFLIGHT.is_file() or not os.access(VRAM_PREFLIGHT, os.X_OK):
        log.error("missing executable VRAM preflight: %s", VRAM_PREFLIGHT)
        return False
    process = await asyncio.create_subprocess_exec(
        str(VRAM_PREFLIGHT),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return False
    if process.returncode != 0:
        log.warning("VRAM preflight rejected cold start: %s", stderr.decode().strip())
        return False
    return True


async def ensure_backend_started() -> None:
    async with _start_stop_lock:
        if GAME_MARKER.exists():
            raise ServiceUnavailable("game_active", "GameMode currently owns the GPU")
        if (blocked := gpu_lease_block()) is not None:
            if await _backend_active():
                await _hard_stop_backend("non-LLM GPU lease became active")
            raise blocked
        if _backend_reachable():
            return
        if not await _run_vram_preflight():
            raise ServiceUnavailable(
                "insufficient_vram",
                "vLLM requires at least 22650 MiB free VRAM for this locked profile",
            )

        log.info("starting %s after successful VRAM preflight", BACKEND_UNIT)
        if await _run_systemctl("start", BACKEND_UNIT, timeout=8) != 0:
            raise ServiceUnavailable("start_failed", "vLLM service refused to start")

        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if GAME_MARKER.exists():
                await _hard_stop_backend("game marker appeared during cold start")
                raise ServiceUnavailable("game_active", "GameMode currently owns the GPU")
            if (blocked := gpu_lease_block()) is not None:
                await _hard_stop_backend("non-LLM GPU lease appeared during cold start")
                raise blocked
            if _backend_reachable():
                record_activity()
                log.info("vLLM backend became reachable")
                return
            if not await _backend_active():
                raise ServiceUnavailable("start_failed", "vLLM exited during cold start")
            await asyncio.sleep(HEALTH_POLL_SECONDS)

        await _hard_stop_backend("75-second cold-start deadline exceeded")
        raise ServiceUnavailable("start_timeout", "vLLM did not become ready within 75 seconds")


async def send_503(writer: asyncio.StreamWriter, error: ServiceUnavailable) -> None:
    body = json.dumps(
        {"error": {"code": error.code, "message": str(error), "type": "service_unavailable"}},
        separators=(",", ":"),
    ).encode("utf-8")
    writer.write(
        b"HTTP/1.1 503 Service Unavailable\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n".encode("ascii")
        + b"Retry-After: 5\r\nConnection: close\r\n\r\n"
        + body
    )
    try:
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            # Renew only because bytes actually moved. Never renew on a quiet
            # keep-alive connection.
            record_activity()
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        writer.close()


async def handle_client(
    client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
) -> None:
    global _active_connections
    peer = client_writer.get_extra_info("peername")
    if GAME_MARKER.exists():
        await send_503(
            client_writer,
            ServiceUnavailable("game_active", "GameMode currently owns the GPU"),
        )
        return
    if (blocked := gpu_lease_block()) is not None:
        await send_503(client_writer, blocked)
        return

    _active_connections += 1
    record_activity()
    log.info("connection opened from %s", peer)
    try:
        try:
            await ensure_backend_started()
        except ServiceUnavailable as error:
            log.warning("rejecting %s: %s", peer, error.code)
            await send_503(client_writer, error)
            return

        if GAME_MARKER.exists():
            await _hard_stop_backend("game marker appeared before proxy splice")
            await send_503(
                client_writer,
                ServiceUnavailable("game_active", "GameMode currently owns the GPU"),
            )
            return
        if (blocked := gpu_lease_block()) is not None:
            await _hard_stop_backend("non-LLM GPU lease appeared before proxy splice")
            await send_503(client_writer, blocked)
            return

        try:
            backend_reader, backend_writer = await asyncio.open_connection(
                BACKEND_HOST, BACKEND_PORT
            )
        except OSError:
            await send_503(
                client_writer,
                ServiceUnavailable("backend_unavailable", "vLLM backend is unavailable"),
            )
            return

        client_to_backend = asyncio.create_task(pipe(client_reader, backend_writer))
        backend_to_client = asyncio.create_task(pipe(backend_reader, client_writer))
        await asyncio.gather(client_to_backend, backend_to_client)
    finally:
        _active_connections = max(0, _active_connections - 1)
        # Closing the connection is real lifecycle activity and atomically
        # arms a fresh hard deadline. It cannot be extended by a client TTL.
        record_activity()
        log.info("connection closed from %s", peer)


async def main() -> None:
    configure_logging()
    if await _backend_active():
        if (blocked := gpu_lease_block()) is not None:
            await _hard_stop_backend(blocked.code)
        else:
        # Never invent a later deadline after a proxy restart. The watchdog
        # will preserve a valid prior state or stop a backend with no state.
            try:
                restored_deadline = restore_locked_deadline()
                if restored_deadline <= time.monotonic_ns():
                    await _hard_stop_backend("proxy restored an already expired deadline")
            except (OSError, ValueError, json.JSONDecodeError) as error:
                await _hard_stop_backend(
                    f"proxy started with invalid or absent deadline state: {error}"
                )
    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or ())
    log.info("listening on %s -> %s:%s", addresses, BACKEND_HOST, BACKEND_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        log.exception("fatal proxy error")
        raise
