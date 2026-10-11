# Dominus LM Studio compatibility gateway

This container preserves the LM Studio API surface used by the fleet while a
llama.cpp router owns model processes. It does not contain or load model
weights itself.

Supported surfaces:

- public `GET /health` liveness and game-interlock state;
- authenticated LM Studio catalog APIs at `GET /api/v0/models` and
  `GET /api/v1/models`;
- LM Studio-compatible model load/unload APIs under both `/api/v0` and
  `/api/v1`;
- OpenAI-compatible `/v1/*` forwarding, including unbuffered SSE streaming;
- removal of the LM Studio-only top-level `ttl` request field;
- a non-overridable 300-second model idle/sleep policy;
- alias-to-llama-router preset translation from `models.lock.json`.

The gateway reads `/config/models.lock.json` by default. The preferred schema
has a top-level `models` array and optional `artifacts` array. Each model needs
an `id`; the migration manifest additionally supplies `aliases`, `router_id`,
`type`, `quantization`, `max_context_length`, and `artifact_ids`. An
artifact-only document is accepted as a compatibility fallback.

`max_context_length` is the model capability advertised in the catalog.
`loaded_context_length` (also accepted as `preset_context_length`) is separate
and reports the actual llama.cpp preset context under `loaded_instances` and
echoed load configuration. The gateway never substitutes one for the other
when both are present.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLAMA_CPP_URL` | `http://llama-router:8080` | Internal router URL |
| `MODEL_LOCK_PATH` | `/config/models.lock.json` | Read-only catalog mount |
| `GAME_ACTIVE_MARKER` | `/run/dominus/game-active` | Existing path blocks loads and inference |
| `GATEWAY_BEARER_TOKEN_FILE` | unset | Preferred client-token secret file |
| `GATEWAY_BEARER_TOKEN` | none | Required client token; startup fails closed when neither token source is set |
| `UPSTREAM_BEARER_TOKEN_FILE` | unset | Optional llama.cpp token secret file |
| `UPSTREAM_BEARER_TOKEN` | unset | Optional llama.cpp token |
| `UPSTREAM_CONNECT_TIMEOUT` | `10` | Upstream connect/write/pool timeout |
| `UPSTREAM_READ_TIMEOUT` | `1800` | Long inference read timeout |
| `UPSTREAM_HEALTH_TIMEOUT` | `2` | Health probe timeout |

The generic `API_BEARER_TOKEN[_FILE]` and `UPSTREAM_API_KEY[_FILE]` names are
also accepted. Secret-file values take precedence. The gateway never forwards
the client credential to llama.cpp, does not include secrets in responses, and
runs Uvicorn without access logs.

Requests that arrive through the Tailscale-bound publish are seen from
docker-proxy's address (`172.19.0.1`, with `127.0.0.1` and `100.64.0.0/10`
accepted for topology changes). Those sources may omit the bearer only for the
surface a token-less `aichat` client uses: `/v1/*`, `GET /api/v0/models` and
its per-model detail, and `GET /api/v1/models` (`_bypass_allowed` in
`gateway/main.py`). Model load/unload, `/api/v1/gpu/lease/*`, and the
`/api/v1/comfy/*` facade require the bearer from every source. A non-ASCII
bearer is a `401`, not a `500`.

Request bodies are read through a 64 MiB bound (`MAX_REQUEST_BODY_BYTES`): a
larger declared `Content-Length`, or a chunked body that crosses the limit, is
refused with `413 payload_too_large` before anything reaches an upstream. The
Compose `mem_limit`/`pids_limit` on this service are the backstop behind it.

`/v1/*` waits at most 120 seconds (`INFERENCE_COORDINATION_WAIT_SECONDS`) for
the coordination lock held by an in-flight request, then returns
`503 inference_busy` with `Retry-After: 5`. `/health` reports `inference_busy`
so a client that stopped reading its stream is distinguishable from a down
router (`upstream` stays `ok` in that case).

While the marker exists, `/v1/*` inference and both load endpoints return 503.
Catalogs and health remain available, and unload remains enabled so the guard
can always free GPU memory.

Every v0/v1 load request has its `ttl` replaced with `300`, including requests
that omit it or try to use `0`, a shorter duration, or a longer duration. The
effective value is returned as `ttl` in the load response. OpenAI inference
requests have their LM Studio-only `ttl` removed, so a client cannot change the
router policy through inference either.

The pinned llama.cpp router is launched with `--sleep-idle-seconds 298`, and its
management port must remain internal to the Compose network. The gateway and
that router setting are the two halves of the guarantee: the gateway prevents
client overrides, while llama.cpp owns the actual inactivity clock and releases
model memory before the hard 300-second ceiling, including its scheduler margin.
(Lowered 2026-08-17 from the original 900s/898s to release large presets like
Dolphin-Mistral-24B faster; see `~/git/vram-guard/NOTES.md`.)

## Verification

From this directory:

```sh
python -m pytest
docker build -t dominus-lmstudio-compat:test .
```

The image is pinned to the linux/amd64 manifest of Python 3.13.7 slim
Bookworm. All direct and transitive production Python dependencies are pinned
in `requirements.lock`.
