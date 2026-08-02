# ADR-0006: Image generation pipeline — Illustrious / NoobAI-XL + Klukai LoRA on dominus-nobara

- **Date:** 2026-04 (formalized 2026-05-16)
- **Updated:** 2026-08-01 (authenticated facade and bounded GPU lease)
- **Status:** Accepted (superseded Animagine XL + Pony)
- **Authors:** jalsarraf

## Context

Klukai needs to generate in-character images on demand (mid-chat
hooks like "show me Klukai in winter outfit"). Generic SDXL produces
generic anime girls — wrong character, wrong outfit, wrong vibe.
The original 2026-04-04 stack was Animagine XL 3.1 + Pony Realism;
character drift was significant, fingers/anatomy unreliable.

## Decision

ComfyUI on `dominus-nobara` with:
- **Base**: NoobAI-XL (Illustrious family) on X: NVMe RAID
- **LoRA**: Klukai IL LoRA — character-specific weights for face,
  hair, eye color, signature outfits
- **Adapters**: PhotoMaker for reference-image continuity
- **Workflow**: pre-built in `ComfyUI/user/default/workflows/`
- **Routing**: companion-core POSTs to the authenticated compatibility gateway
  at `dominus-nobara:1234/api/v1/comfy`; ComfyUI's `:8188` socket is internal
  and has no host mapping
- **Arbitration**: companion-core acquires a fixed, bounded GPU lease; the
  gateway drains/unloads LLM inference before permitting the internal ComfyUI
  request and frees image VRAM before release

## Consequences

- **VRAM contention with chat LLM**: the lease serializes image generation and
  LLM/voice loads. The next chat may pay a cold-start cost after image cleanup.
- **Memory archive integration**: every generated image is saved to
  `companion-images` volume + `companion_memories` table via
  `memory_archive.save_image()`. Klukai's curated "photo album"
  (per `project_memory_archive.md`) draws from these.
- **Annotation pipeline** (per `feedback_gptoss_for_memories.md`):
  gpt-oss-20b selects images, dolphin-24b annotates them.
- **Failure mode**: image gen down = chat continues without inline
  images. P3 severity per `docs/runbooks/comfyui-down.md`.
- **Model file size**: Klukai LoRA + base model ~10GB. Restore from
  the immutable RAID model release takes hours. Do not delete it accidentally.

## Alternatives considered

- **Animagine XL + Pony Realism** (original): rejected — character
  drift too high.
- **Cloud image API (Replicate / OpenAI)**: rejected — privacy
  (Commander-only content), cost, no character LoRA support.
- **Stable Diffusion 3 / Flux**: rejected — Klukai LoRAs aren't
  available; would need retraining at significant compute cost.

## Related

- `app/image_gen.py`
- `reference_illustrious.md` (global CLAUDE.md)
- `feedback_comfyui_port.md`
- `docs/runbooks/comfyui-down.md`
- `project_memory_archive.md`
- ADR-0004 (local model routing — shares the GPU)
- ADR-0007 (voice also on the dominus-nobara GPU)
