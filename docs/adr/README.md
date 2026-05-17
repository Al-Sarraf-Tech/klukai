# Architecture Decision Records — klukai

Each ADR captures one load-bearing decision: context, decision,
status, consequences. Format follows [MADR-lite](https://adr.github.io/madr/).

## Index

| ADR | Title | Status | Phase |
|---|---|---|---|
| [0001](0001-s-tier-uplift.md) | Adopt S+ tier uplift roadmap | Accepted | Spec |
| [0002](0002-amarillo-dominus-split.md) | Compute split: amarillo (core/gateway) + dominus (voice/GPU) | Accepted | Origin |
| [0003](0003-three-tier-memory.md) | Three-tier memory: Redis → Qdrant → PostgreSQL | Accepted | Origin |
| [0004](0004-lm-studio-routing.md) | LM Studio model routing (gemma-4 / dolphin / gpt-oss) | Accepted | 2026-04 |
| [0005](0005-affection-taxonomy.md) | Affection level 0-9 with distinct speech patterns | Accepted | Origin |
| [0006](0006-image-gen-pipeline.md) | Illustrious + Klukai LoRA on dominus ComfyUI | Accepted | 2026-04 |
| [0007](0007-voice-on-dominus.md) | XTTS v2 on dominus (RTX 3090 + CUDA) | Accepted | Origin |
| [0008](0008-audit-chain-hmac.md) | HMAC-chained audit log for tamper detection | Accepted | 2026-04 |
| [0009](0009-cloudflare-nginx-gateway.md) | Cloudflare → nginx gateway → companion-core | Accepted | Origin |
| [0010](0010-flutter-pwa-base-href.md) | Flutter PWA at `/app/` base-href (service worker scope) | Accepted | 2026-04 |
| [0011](0011-character-rules.md) | Klukai is T-Doll; Commander is HUMAN (canonical rules) | Accepted | Origin |
| [0012](0012-memory-seeding-cadence.md) | Memory archive seeding every 2 days, 3-6 AM | Accepted | 2026-04 |
| [0013](0013-klukai-vs-kairi-separation.md) | klukai and kairi are separate characters, separate data | Accepted | 2026-04 |
| [0014](0014-offsite-backup.md) | Off-site backup: amarillo → dominus nightly tar | Accepted | 2026-04 |
| [0015](0015-wsl2-decommissioned.md) | wsl2 is NOT a klukai deployment target | Accepted | 2026-04 |

## Conventions

- Filenames: `NNNN-kebab-case-title.md`.
- New ADRs are numbered sequentially. Never renumber.
- Status: `Proposed` → `Accepted` → optional `Superseded by NNNN` or `Deprecated`.
- Supersedes are bidirectional: the new ADR links the old, the old's
  status updates with the supersession.
- One decision per ADR. If a PR introduces multiple decisions, one ADR
  per decision.

## When to write an ADR

- Choosing between competing technologies / patterns / vendors.
- Setting an invariant (e.g., "Klukai's Commander is always HUMAN").
- Reversing a prior decision.
- Documenting a constraint that future maintainers might otherwise
  fight (e.g., "no macOS builds", "no `actions/attest-build-provenance`").

If a code reviewer would reasonably ask "why this and not the obvious
alternative?", that's an ADR-worthy decision.

## Process

1. Pick the next ADR number.
2. Copy `_template.md` (TBD) or model after an existing ADR.
3. Open as PR with status `Proposed`.
4. Reach alignment, flip to `Accepted`, merge.
5. If the decision is later reversed, add a new ADR with the
   reversal, update both for cross-linking.
