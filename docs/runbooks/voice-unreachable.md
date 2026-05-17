# Runbook: Voice Service Unreachable (dominus)

**Severity:** P2 (TTS disabled; chat continues text-only)
**SLO breach:** `/api/voice/*` returns 5xx; chat responses lose audio.

## Symptom

- `/health` shows `voice: "down"` in subsystem detail
- companion-core logs: `httpx.ConnectError` to `192.168.50.2:8301`
- Flutter PWA shows "Voice unavailable" indicator
- TTS requests return 503

## Immediate action (< 5 min)

1. Verify dominus is reachable from amarillo:
   ```bash
   ping -c 3 192.168.50.2
   ssh dominus 'echo OK'
   ```
2. Check companion-voice container on dominus:
   ```bash
   ssh dominus 'docker ps --filter name=companion-voice'
   ssh dominus 'docker logs --tail=200 companion-voice'
   ```
3. If exited: `ssh dominus 'docker restart companion-voice'`.
4. Per `feedback_dominus_voice_port.md`, voice periodically loses the
   `:8301` binding. Fix:
   ```bash
   ssh dominus 'docker rm -f companion-voice && cd ~/git/klukai && docker compose -f docker-compose.voice.yml up -d'
   ```

## Root-cause investigation

| Symptom | Likely cause | Fix |
|---|---|---|
| Container up, port not bound | Known dominus voice port bug | `rm -f` + recreate (see above) |
| Container exited, CUDA error | XTTS CUDA OOM | Restart; review GPU sharing with ComfyUI |
| Container OK, slow response | Model loading or busy queue | Wait; or `ssh dominus 'nvidia-smi'` to confirm |
| Cannot reach 192.168.50.2 | LAN issue, dominus down | Power-cycle dominus if needed |

## Verification after fix

1. `curl -sf http://192.168.50.2:8301/health` returns 200.
2. Send a chat turn; verify audio plays in PWA.
3. Check companion-core logs for successful TTS handoff.

## Graceful degradation

Until Phase 4 circuit breaker lands, voice down = text-only chat. Users
see "Voice unavailable" toast but conversation continues. **No chat
memory or affection state is at risk.**

## Per `feedback_lan_transfers.md`

Always use **LAN** (192.168.50.2) for voice service, not Tailscale.
Tailscale path is for SSH; LAN path is for low-latency TTS audio.

## Post-incident

- If port-binding bug recurred, file ticket to investigate dominus Docker
  networking (root cause unknown — known recurring issue).
- Run `make health` to confirm full stack.

## Related

- ADR-0007: Voice on dominus only (RTX 3090 + CUDA)
- `feedback_dominus_voice_port.md`
- `feedback_lan_transfers.md`
- `docker-compose.voice.yml` (on dominus)
