# Runbook: LM Studio-Compatible Gateway Cold Start

**Severity:** P3 (slow first chat after idle; later requests are normal)

**SLO breach:** `/api/chat/turn` first-token latency may temporarily exceed its target.

## Expected behavior

`dominus-nobara` no longer runs the LM Studio application. The CPU-only
`lmstudio-compat` service preserves its authenticated API on Tailscale port
`1234`, while the internal `llama-router` service runs the pinned llama.cpp
backend and loads one locked preset at a time.

An idle model is unloaded no later than 900 seconds after its last inference.
The current llama.cpp deadline is slightly lower than 900 seconds to allow for
the runtime's polling interval. The gateway strips client-supplied `ttl`
values, so a caller cannot extend residency. A cold request after that unload
can take tens of seconds while weights return to RAM and VRAM; this is
intentional.

## Symptoms

- The first chat after at least 15 minutes of inactivity is slow.
- Later requests to the same model are fast.
- `GET /health` reports the gateway and router state, but does not load a model.
- An authenticated catalog request reports the selected model as loading or
  not loaded.

## Immediate checks

Run API checks from an enrolled Tailnet host. Keep the rotated token out of
shell history and logs.

```bash
read -r -s -p 'LM gateway token: ' LM_STUDIO_TOKEN; printf '\n'
export LM_STUDIO_TOKEN
curl --fail http://100.107.121.5:1234/health | jq .
curl --fail \
  -H "Authorization: Bearer ${LM_STUDIO_TOKEN:?token is not set}" \
  http://100.107.121.5:1234/api/v0/models | jq .
```

On `dominus-nobara`, inspect the canonical user unit and Compose services:

```bash
cd /mnt/nvmer0/services/ai-stack/source/klukai/ops/dominus-nobara
systemctl --user status dominus-ai-stack.service --no-pager
docker compose \
  --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --file compose.yaml ps lmstudio-compat llama-router
docker compose \
  --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --file compose.yaml logs --tail=200 lmstudio-compat llama-router
nvidia-smi
```

If both services are healthy and the delay followed a true idle period, take
no action. Do not add a keepalive or increase the TTL.

## Failure diagnosis

| Result | Meaning | Action |
| --- | --- | --- |
| First request alone is slow | Normal lazy model load | Wait for that request to finish |
| HTTP 401 | Missing or stale bearer token | Re-read the rotated `LM_STUDIO_TOKEN`; do not disable auth |
| HTTP 503 with `game_active` | GameMode owns the GPU | Wait for the game to exit; do not bypass the guard |
| `/health` is degraded | `llama-router` is unavailable or starting | Inspect the two service logs and the model release mount |
| Every request reloads | Router restart, load failure, or model swap | Check container restart counts, logs, and `nvidia-smi` |
| Model remains resident after 900 seconds idle | TTL safety contract failed | Stop inference and escalate; verify the fixed llama.cpp command and gateway image |

To deliberately pre-load a locked alias for a scheduled job, use the
compatibility API. Pre-loading still obeys the hard idle ceiling:

```bash
curl --fail \
  -H "Authorization: Bearer ${LM_STUDIO_TOKEN:?token is not set}" \
  -H 'Content-Type: application/json' \
  --data '{"model":"cognitivecomputations_dolphin-mistral-24b-venice-edition"}' \
  http://100.107.121.5:1234/api/v0/models/load | jq .
```

## Verification

1. Send one request and confirm it completes with the requested model ID.
2. Confirm at most one LLM preset is resident.
3. Make no inference requests for 900 seconds; health and catalog checks do
   not reset the inference idle timer.
4. Confirm the model has unloaded and GPU memory has returned. An unload a few
   seconds early is expected; an unload later than 900 seconds is a failure.

The complete rebuild and acceptance procedure is in
`ops/dominus-nobara/RUNBOOK.md`.
