# Runbook: LM Studio Cold Start

**Severity:** P3 (slow first chat after idle; subsequent chats normal)
**SLO breach:** `/api/chat/turn` first-token p99 may breach 2s target temporarily.

## Symptom

- First chat after a long idle is slow (15-60s to first token)
- Subsequent chats are normal (sub-second)
- companion-core logs show LM Studio response time >10s
- LM Studio dashboard (192.168.50.2:1234) shows model loading

## Why it happens

Per the 2026-04-20 commit `551906e perf(llm): load-on-demand with 600s TTL`:

- LM Studio JIT-unloads models after 600s of idle.
- First request after unload triggers model load (~10-60s depending on
  size: gemma-4 < dolphin-24b < gpt-oss-20b).
- This is **intentional behavior** — VRAM contention with image gen
  required load-on-demand. See `feedback_llm_load_on_demand.md`.

## Immediate action (none for end-user-visible cold start)

This is expected when the system has been idle. The first request "pays
the cold-start tax." No action needed.

However, if cold-start is happening *during active hours* (not after long
idle), investigate:

1. Check LM Studio dashboard for model loaded state:
   ```bash
   ssh dominus 'curl -sf http://localhost:1234/v1/models | jq'
   ```
2. Check VRAM pressure:
   ```bash
   ssh dominus 'nvidia-smi --query-gpu=memory.used,memory.total --format=csv'
   ```
3. If VRAM full from image gen, model was evicted. Wait for image gen to
   complete or pre-warm:
   ```bash
   ssh dominus 'curl -X POST http://localhost:1234/v1/models/dolphin-2.9.4-llama3.1-8b/load'
   ```

## Root-cause investigation

| Symptom | Likely cause | Fix |
|---|---|---|
| Cold start mid-session | VRAM evicted by image gen | Pre-warm before image gen; throttle image gen |
| Cold start every request | TTL set too low | Verify `LM_STUDIO_TTL_SECONDS=600` env |
| Cold start across all models | LM Studio service restart | `ssh dominus systemctl status lm-studio.service` |

## Verification after fix

1. Send 3 chat turns; verify first <8s p99, subsequent <1s.
2. Confirm LM Studio dashboard shows model loaded.

## Out-of-scope

Cold starts during true idle (e.g., morning after no overnight activity)
are **expected and acceptable**. Do not optimize away — VRAM is finite,
and image gen + chat must coexist on the same GPU.

## Related

- ADR-0004: LM Studio routing
- `feedback_llm_load_on_demand.md`
- `feedback_model_routing.md`
- Commit `551906e` — load-on-demand TTL
