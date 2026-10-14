# Amarillo local XTTS on Intel Arc

This is the owner-authorized local voice release from the 2026-10-14 Arc A380
experiment. It uses the existing verified XTTS v2 snapshot and XPU virtual
environment. It does not install packages, download model files, change Granite,
or alter the Dominus CUDA service or its immutable voice reference.

Klukai v1.1.0 retains this pinned voice release. Its runtime versions and
artifact identity come from [artifacts.lock.json](artifacts.lock.json),
[requirements-supervisor.lock](requirements-supervisor.lock), and
[requirements-worker.lock](requirements-worker.lock), not the core/PWA version.
Provider changes for chat, dreams, or pictures do not change the voice model.

The native service is a small Python HTTP shell and one lazily started child.
Only the child imports Torch or loads XTTS. It conditions the selected reference
on CPU, moves the model and conditioning to `xpu:0` in FP16, and retains them for
at most 300 idle seconds. XTTS's null positional embeddings are adapted to keep
FP16 zeros. `reference/adoption.json` records the selected 6.536–13.402-second
excerpt; `artifacts.lock.json` records its bytes, model bytes, package versions,
Python versions, and the two XTTS source files underlying the adapter.

This release speaks **English text in the selected Japanese reference voice**.
Japanese text is rejected with 422 before any worker starts: the tested virtual
environment does not contain XTTS's required `cutlet` tokenizer dependencies.
The existing voice-letter default is English. STT remains a separate service;
this endpoint advertises `stt_available: false`.

## Core configuration

The current deployment selects `VOICE_BACKEND=amarillo-xpu` and sets `VOICE_URL`
to this service's approved private-mesh endpoint, using the existing matching
`VOICE_API_TOKEN`. Keep `VOICE_STT_URL` on the separate Dominus transcription
endpoint; Arc does not serve STT. The repository's base Compose default remains
`dominus`, so selecting Arc requires explicit core configuration.

Clients use core's authenticated `POST /api/tts`, poll
`GET /api/tts/jobs/{job_id}`, and cancel with `DELETE /api/tts/jobs/{job_id}`.
Core normalizes the native service's JSON/base64 response and owns queueing and
user access. Browser clients never call this native service directly or receive
its bearer token.

Arc voice can accompany either the authorized Sonnet interactive primary or an
entirely local Dominus LLM/ComfyUI setup. See the
[local GPU configuration](../../../README.md#use-local-gpus) for that selection.
Cloud creative providers remain separately disabled by default; changing them
does not start this worker, start Dominus, or bypass either host's guards.

## Runtime contract

- `GET /health`: public, model-free status; `tts_ready`, `busy`, `lazy_loading`,
  `voice_model_ttl`, and `sample_rate: 24000`.
- `POST /tts`: `Authorization: Bearer <VOICE_API_TOKEN>`, JSON
  `{"text":"Commander, this is Klukai.","language":"en"}`. Accepts 1–500
  characters and returns `{"audio":"<base64 WAV>","sample_rate":24000}`.
  WAV is mono 16-bit PCM at 24 kHz. It is not the binary response used by
  Dominus. Concurrent requests receive 429; there is no hidden shell queue.
- `POST /unload`: bearer required; cancels current work, reaps the complete
  worker process group, and responds only after it is gone.

Core's local asynchronous jobs own queuing, user authorization and cancellation.
Closing its upstream HTTP connection causes the shell to terminate and reap the
worker. The shell polls disconnects and CPU/GPU temperatures at most every
250 ms and imposes a 600-second total request deadline, including cold load.
Any reading at or above 68 C, missing required telemetry, worker failure,
cancellation, or invalid output kills the child. A failed model is never reused.
Idle unload and service stop also terminate the worker; process exit releases
both CPU and GPU memory. Auth failures and health checks never spawn a worker.

The model checks at least 1,280 MiB global free XPU memory before loading. This
protects the observed ~1,005 MiB model/short-synthesis peak with a margin but is
not an exclusive lease against other applications. Granite stays running.
Thermal and process limits remain active during inference. Local Arc throughput
is slow: the selected-reference example needed 32.572 seconds for 6.08 seconds
of audio after an earlier synthesis; the first example needed 66.918 seconds,
and CPU checkpoint load took 20.442 seconds. Those are observations, not latency
limits. Public clients must use core's submit/poll interface.

## Installation without fetching or building

These are the existing release's deployment instructions. Publishing a new
core/PWA version does not itself authorize installation or a GPU service start.

Prerequisites already present on the tested Amarillo host:

- `/usr/bin/python3` with the versions in `requirements-supervisor.lock`;
- `~/.cache/klukai/xtts-xpu-venv/bin/python` with the exact inventory in
  `requirements-worker.lock` (the environment has no pip; do not install into it);
- `~/.cache/klukai/xtts-v2/6c2b0d75eae4b7047358e3b6bd9325f857d43f77`;
- access to `/dev/dri/renderD128`, CPU `coretemp`, and Intel `i915`/`xe` sensors;
- Tailscale address `100.111.198.19`.

The checked-in unit assumes the release has been copied into
`~/git/klukai/ops/amarillo/voice_xpu`. When deploying from a worktree, update only
its `WorkingDirectory` and `ExecStart` to the reviewed release path; keep all
resource limits, address, process-group and environment settings intact.

Prepare a mode-0700 `~/.config/klukai` directory and mode-0600
`~/.config/klukai/voice-xpu.env` outside the repository. Put the existing matching
`VOICE_API_TOKEN` there without printing it. Optional `VOICE_XPU_PYTHON` and
`XTTS_MODEL_DIR` override the two verified cache paths, not artifact contents.
Create the writable cache directory `~/.cache/klukai/voice-xpu-runtime` with
mode 0700 before enabling the unit.

Run the read-only preflight from the reviewed release directory:

```sh
/usr/bin/python3 launch.py --verify-only
```

It verifies release source, supervisor dependencies, and model/reference hashes;
it imports no Torch and starts no service. The worker independently checks its
Python/dependency inventory, XTTS source hashes, model and reference immediately
before loading. Version drift or changed artifact bytes fail closed. The lock
files record observed versions and sources, not hashes of downloaded wheels.
No install or update command is provided by this release.

Copy `../systemd/klukai-voice-xpu.service` from the Amarillo directory to
`~/.config/systemd/user/`, then daemon-reload and enable/start that one unit as
part of the authorized deployment. The listener binds only
`100.111.198.19:8301`; if that address is absent at boot, systemd retries every
five seconds. It never binds to `0.0.0.0`. The service has an 8 GiB RAM cap,
no swap, a 200% CPU cap, a read-only filesystem apart from its own cache and
private temp directory, and control-group cleanup on exit. Host `LD_*`
injection variables are removed, and the worker does not inherit the voice
bearer token. There are no changes to other GPU services.

## Verification and rollback

Run the focused process/HTTP tests without inference:

```sh
python3 -m pytest -q ops/amarillo/tests/test_voice_xpu.py
```

Deployment checks must include health from **inside companion-core**, missing
and incorrect bearer rejection, one authenticated synthesis via the core job
path, decoded 24 kHz nonempty WAV, explicit unload, idle expiry, and unchanged
Granite service/PID. A host-local curl alone does not prove Docker reachability.
Check journal output for thermal, dependency, model or worker failures without
logging credentials or request text. Real thermal/cancellation tests should
use controlled test inputs; do not deliberately overheat the GPU.

Rollback selects the previous explicit voice backend/URL in core and stops
`klukai-voice-xpu.service`. Stopping it reaps its worker. It does not start
Dominus; while Dominus is powered off, the old backend remains unavailable.
The original Dominus artifact lock/reference and the retired top-level Compose
file are unchanged.
