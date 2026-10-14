# Klukai

Read `docs/handoff/2026-09-25-dominus-tailscale-publish.md` before touching the Dominus GPU stack, Docker startup, or the chat path to the RTX 3090.

Powering that computer off is normal. On boot, Docker can restore the AI containers before Tailscale has `100.107.121.5` and then never bind `1234`, `8301`, or `8390`. A healthy container and `docker restart` do not mean Amarillo can reach her. The repair is `ops/dominus-nobara/scripts/repair-tailscale-publish.sh`. It must keep refusing to run while `/run/user/1000/dominus-gpu/game-active` exists, and it must not recreate `llama-router` or `comfyui`.

On 2026-10-14 the owner explicitly authorized paid Anthropic API use for
Klukai: Sonnet 5.5 with medium adaptive thinking is the interactive primary,
and Dominus is secondary. This supersedes earlier local-only instructions for
this path only. Background local jobs and Dominus gaming/idle guards remain.
See `docs/superpowers/specs/2026-10-14-local-voice-sonnet.md`.

## Voice source baseline

Read `docker/voice/reference/README.md` for the owner's selected Klukai voice
baseline. The complete source video is
`docker/voice/reference/baseline/3tJ1ZE11J34.mp4`; its extracted mono PCM audio is
`docker/voice/reference/baseline/3tJ1ZE11J34.wav`. Source attribution, media
properties, and digests are in `docker/voice/reference/baseline/source.json`.
Verify the files with
`cd docker/voice/reference/baseline && sha256sum -c SHA256SUMS`.

This is the source recording for listening, comparison, and curating short
conditioning clips. Dominus XTTS retains its separately pinned
`klukai_reference.wav` artifact. The Amarillo Arc release uses its own pinned
excerpt and adoption record under `ops/amarillo/voice_xpu`; do not change the
Dominus lock to deploy that release.
