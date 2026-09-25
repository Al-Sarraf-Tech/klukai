# Klukai

Read `docs/handoff/2026-09-25-dominus-tailscale-publish.md` before touching the Dominus GPU stack, Docker startup, or the chat path to the RTX 3090.

Powering that computer off is normal. On boot, Docker can restore the AI containers before Tailscale has `100.107.121.5` and then never bind `1234`, `8301`, or `8390`. A healthy container and `docker restart` do not mean Amarillo can reach her. The repair is `ops/dominus-nobara/scripts/repair-tailscale-publish.sh`. It must keep refusing to run while `/run/user/1000/dominus-gpu/game-active` exists, and it must not recreate `llama-router` or `comfyui`. No cloud LLM fallback.
