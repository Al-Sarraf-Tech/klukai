# Runbook: ComfyUI Image Generation Down

**Severity:** P3 (image generation disabled; chat remains available)

**SLO breach:** `/api/images/generate` returns 5xx or produces no image.

## Symptoms

- Image-producing chat turns return text only.
- `companion-core` reports the gateway's ComfyUI status as down.
- Image requests through `100.107.121.5:1234/api/v1/comfy` fail after lease
  acquisition.

## Immediate checks

From `amarillo`:

```bash
tailscale ping -c 3 dominus-nobara
curl --fail http://100.107.121.5:1234/health | jq '.comfyui_status'
```

On `dominus-nobara`, inspect the canonical Compose service:

```bash
cd /mnt/nvmer0/services/ai-stack/source/klukai/ops/dominus-nobara
test ! -e /run/user/1000/dominus-gpu/game-active
systemctl --user status dominus-ai-stack.service --no-pager
docker compose \
  --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --file compose.yaml ps comfyui
docker compose \
  --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --file compose.yaml logs --tail=200 comfyui
```

If the game marker exists, ComfyUI is intentionally stopped. Wait for the
GameMode end hook. Otherwise, recreate only the canonical service:

```bash
docker compose \
  --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --file compose.yaml up --detach --no-build --no-deps --force-recreate comfyui
```

## Failure diagnosis

| Result | Meaning | Action |
| --- | --- | --- |
| Game marker exists | GameMode owns the GPU | Wait; do not bypass the guard |
| CUDA OOM while loading a workflow | Another GPU model is resident | Unload it through its supported API or wait for its job/idle TTL |
| Gateway reports `comfyui_status` down | Canonical container is stopped or unhealthy | Inspect logs and recreate `comfyui` |
| Client uses port 8188 or 8388 | It bypasses the lease/authentication boundary | Set Klukai core to `http://100.107.121.5:1234/api/v1/comfy` |
| Checkpoint or LoRA is absent | Immutable release is incomplete | Verify `models/current` against `models.lock.json` |
| Output cannot be written | RAID data path ownership or mount failed | Verify `/mnt/nvmer0` and the explicit `/data` bind mounts |

ComfyUI listens only on the internal Compose network at container port `8188`.
The gateway facade at `100.107.121.5:1234/api/v1/comfy` requires both the
rotated bearer credential and a matching bounded GPU lease; direct host access
is intentionally absent.

## Verification

1. Confirm gateway health reports `comfyui_status: ok`; inspect
   `/system_stats` and `/object_info` only from inside the container network.
2. Run one NoobAI/Illustrious workflow with
   `Klukai_GFL2_IL-03.safetensors` and one SDXL Turbo workflow.
3. Confirm generated files land under
   `/mnt/nvmer0/services/ai-stack/data/comfyui/output`.
4. Confirm chat remains usable while image generation is unavailable or busy.

Models are mounted read-only from the verified release. Do not download
replacement weights inside ComfyUI; restore or restage them through the model
lock procedure in `ops/dominus-nobara/RUNBOOK.md`.
