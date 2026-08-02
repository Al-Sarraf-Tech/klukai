# Runbook: Voice Service Unreachable (`dominus-nobara`)

**Severity:** P2 (TTS disabled; chat continues text-only)

**SLO breach:** `/api/voice/*` returns 5xx or chat responses lose audio.

## Symptoms

- Companion health reports the voice subsystem as down.
- `companion-core` cannot connect to `100.107.121.5:8301`.
- The PWA shows its voice-unavailable state.
- TTS requests return 503 while text chat continues.

## Immediate checks

From `amarillo`, prove the Tailnet path and check the public, model-free health
endpoint:

```bash
tailscale ping -c 3 dominus-nobara
curl --fail http://100.107.121.5:8301/health | jq .
```

On `dominus-nobara`, use the canonical stack rather than the retired
top-level `docker-compose.voice.yml`:

```bash
cd /mnt/nvmer0/services/ai-stack/source/klukai/ops/dominus-nobara
test ! -e /run/user/1000/dominus-gpu/game-active
systemctl --user status dominus-ai-stack.service --no-pager
docker compose \
  --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --file compose.yaml ps companion-voice
docker compose \
  --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --file compose.yaml logs --tail=200 companion-voice
```

If the game marker exists, the stopped voice container is intentional. Wait
for GameMode's end hook; never start a GPU service around the guard.

If there is no active game and only voice needs recovery, recreate the
canonical service without building or touching other projects:

```bash
docker compose \
  --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --file compose.yaml up --detach --no-build --no-deps --force-recreate \
  companion-voice
```

Do not use `docker rm`, the retired Compose file, `down -v`, or a manually
published port. The canonical service alone owns `100.107.121.5:8301`.

## Failure diagnosis

| Result | Meaning | Action |
| --- | --- | --- |
| Game marker exists | GPU services were quiesced for a game | Wait for the game to exit |
| Container is absent or exited | Canonical service failed to start | Inspect Compose logs, then recreate as above |
| CUDA OOM on first TTS request | Another GPU workload is resident | Let it finish or unload it through its supported API |
| Health works; TTS returns 401 | Missing or stale `VOICE_API_TOKEN` | Restore the rotated matching token on client and server |
| Port 8301 is already in use | A legacy/manual voice container is competing | Identify it with `ss -lntp`; do not delete until ownership is confirmed |
| Model or reference WAV is missing | Immutable release is incomplete | Re-run model-release verification; do not download in the container |

## Verification

1. Confirm `GET http://100.107.121.5:8301/health` returns `status: ok` and
   `lazy_loading: true` without loading XTTS.
2. Send one authenticated `/tts` request and verify audio playback.
3. Call authenticated `POST /unload` and confirm XTTS GPU memory is released.
4. Send a Klukai chat turn and verify the core-to-voice handoff succeeds.

All access is through Tailscale (`100.107.121.5` or
`dominus-nobara.tail9bdca.ts.net`). The host port is bound only to the
Tailscale address. TTS, STT, and unload endpoints require the rotated bearer
token; `/health` is intentionally model-free.

The complete stack procedure is in `ops/dominus-nobara/RUNBOOK.md`.
