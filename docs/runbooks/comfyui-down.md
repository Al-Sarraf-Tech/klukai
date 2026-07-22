# Runbook: ComfyUI Image Generation Down

**Severity:** P3 (image gen disabled; chat continues without inline images)
**SLO breach:** `/api/images/generate` returns 5xx.

## Symptom

- Chat responses that should include images return text-only
- companion-core logs: `httpx.ConnectError` to `dominus:8388`
- ComfyUI dashboard at http://dominus:8388 doesn't load

## Immediate action (< 5 min)

1. Verify ComfyUI is reachable:
   ```bash
   tailscale ping -c 3 dominus
   curl -sf http://dominus:8388/system_stats
   ```
2. Check container on dominus:
   ```bash
   ssh dominus 'docker ps --filter name=comfyui'
   ssh dominus 'docker logs --tail=200 comfyui'
   ```
3. Per `feedback_comfyui_port.md`: container maps **8188 internal → 8388
   external**. If port is wrong, fix `docker-compose.yml` and recreate.
4. If exited: restart:
   ```bash
   ssh dominus 'docker restart comfyui'
   ```

## Root-cause investigation

| Symptom | Likely cause | Fix |
|---|---|---|
| Container exited, CUDA OOM | LM Studio + image gen contention | Wait for chat to settle; restart |
| Port mismatch | `feedback_comfyui_port.md` bug | Verify `8388:8188` (host:container) mapping in compose |
| Workflow load error | Missing model in `models/checkpoints/` | Check Illustrious + Klukai LoRA presence |
| Slow generation | VRAM pressure | Normal under load; throttle |

## Verification after fix

1. `curl -sf http://dominus:8388/system_stats` returns model list.
2. Trigger an image gen via chat ("show me Klukai in winter outfit").
3. Verify image appears within 15s p95 (per SLO).

## Per `reference_illustrious.md`

Image generation pipeline:
- NoobAI-XL base + Klukai IL LoRA on X: NVMe RAID
- 5s VRAM-contention delay built in (avoids LLM clash)
- Workflows live in `ComfyUI/user/default/workflows/`

If a model file is missing, restore from dominus model storage —
re-downloading takes hours.

## Out-of-scope

Image gen is **non-critical** to klukai's core function (chat). Down for
hours = acceptable. Users see "image unavailable" indicator. Until Phase
4 circuit breaker, image-gen down = a few extra exception logs.

## Related

- ADR-0006: Image gen pipeline (Illustrious + Klukai LoRA)
- `reference_illustrious.md`
- `feedback_comfyui_port.md`
- `app/image_gen.py`
