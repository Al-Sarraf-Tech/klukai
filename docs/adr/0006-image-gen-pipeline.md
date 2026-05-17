# ADR-0006: Image generation pipeline — Illustrious / NoobAI-XL + Klukai LoRA on dominus

- **Date:** 2026-04 (formalized 2026-05-16)
- **Status:** Accepted (superseded Animagine XL + Pony)
- **Authors:** jalsarraf

## Context

Klukai needs to generate in-character images on demand (mid-chat
hooks like "show me Klukai in winter outfit"). Generic SDXL produces
generic anime girls — wrong character, wrong outfit, wrong vibe.
The original 2026-04-04 stack was Animagine XL 3.1 + Pony Realism;
character drift was significant, fingers/anatomy unreliable.

## Decision

ComfyUI on dominus with:
- **Base**: NoobAI-XL (Illustrious family) on X: NVMe RAID
- **LoRA**: Klukai IL LoRA — character-specific weights for face,
  hair, eye color, signature outfits
- **Adapters**: PhotoMaker for reference-image continuity
- **Workflow**: pre-built in `ComfyUI/user/default/workflows/`
- **Routing**: companion-core POSTs to ComfyUI at
  `192.168.50.2:8388` (per `feedback_comfyui_port.md` external:internal
  mapping = 8388:8188)
- **Cooldown**: 5s delay after each chat response before image gen
  starts, to avoid VRAM contention with LM Studio

## Consequences

- **VRAM contention with chat LLM**: image gen evicts dolphin/gpt-oss
  from VRAM. LM Studio's JIT TTL (ADR-0004) re-loads on next chat. The
  cold-start tax is the cost.
- **Memory archive integration**: every generated image is saved to
  `companion-images` volume + `companion_memories` table via
  `memory_archive.save_image()`. Klukai's curated "photo album"
  (per `project_memory_archive.md`) draws from these.
- **Annotation pipeline** (per `feedback_gptoss_for_memories.md`):
  gpt-oss-20b selects images, dolphin-24b annotates them.
- **Failure mode**: image gen down = chat continues without inline
  images. P3 severity per `docs/runbooks/comfyui-down.md`.
- **Model file size**: Klukai LoRA + base model ~10GB. Restore from
  dominus model storage takes hours. Don't delete by accident.

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
- ADR-0004 (LM Studio routing — shares the GPU)
- ADR-0007 (voice also on dominus GPU)
