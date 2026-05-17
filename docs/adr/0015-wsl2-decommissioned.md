# ADR-0015: wsl2 is NOT a klukai deployment target

- **Date:** 2026-04-20 (formalized 2026-05-16)
- **Status:** Accepted
- **Authors:** jalsarraf

## Context

Early in klukai's history (2026-04 origin period), wsl2 was
considered as a possible host for parts of the stack — convenient
for development, Linux toolchain available on Windows. As the project
matured, dominus (Windows + RTX 3090 + Tailscale) and amarillo
(Linux server) emerged as the clear deployment targets.

wsl2 lingered as a "maybe we'll deploy something here" possibility
for ~2 months, creating confusion about where services should run
and leading to drift in compose files.

## Decision

**wsl2 is NOT a klukai deployment target.** Per commit `8654573
chore(ops): decommission wsl2 as klukai deployment target`
(2026-04-20) and the global CLAUDE.md `feedback_wsl2_decommissioned.md`
memory.

klukai topology is exactly two hosts:
- **amarillo** (Linux server) — core, gateway, PG, Redis, Qdrant
- **dominus** (Windows + RTX 3090) — voice, ComfyUI, LM Studio

That's it. Future hosts require a new ADR.

## Consequences

- **Simplifies operations**: only two failure domains, two backup
  destinations, two systemd manifests to maintain.
- **Compose files** are scoped to either amarillo or dominus —
  no wsl2 overrides.
- **CI runner topology** (per global CLAUDE.md) excludes wsl2 from
  klukai-specific jobs.
- **Personal Windows machine** can still run klukai dev clients
  (`make build-pwa` works anywhere with flutter installed) but no
  klukai server-side services live there.

## Alternatives considered

- **Keep wsl2 as a third deployment target**: rejected — added
  failure domain with no resource benefit (wsl2 has no GPU,
  no production network exposure).
- **Migrate dominus services to wsl2**: rejected — would require
  CUDA-in-wsl2 setup with marginal gains, plus voice/image-gen
  latency penalty from the WSL kernel hop.

## Related

- Commit `8654573` (decommission commit)
- `feedback_wsl2_decommissioned.md` (global CLAUDE.md)
- ADR-0002 (amarillo/dominus split — the canonical topology)
- Phase 2 spec §3 Non-Goals (no third host)
