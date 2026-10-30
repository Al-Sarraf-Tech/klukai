# Klukai

**Cloud AI only (owner decision, 2026-10-26).** Klukai runs no inference on
Dominus-Nobara or Amarillo. Voice in and out is Fish Audio. There is no local
fallback of any kind. Design: `docs/superpowers/specs/2026-10-26-cloud-only-ai-design.md`.

**Grok account login (owner decision, 2026-10-30).** Chat, background text,
pictures, and the picture checker run through the official Grok CLI
(`grok-4.7`, effort `medium`) on the owner's account login. No xAI API key,
no paid credits, and no fallback to a paid API. Fish voice is unchanged.
Do not reintroduce LM Studio, Dominus voice, ComfyUI, the GPU lease, local
embeddings or Qdrant into `docker/core`; `tests/test_cloud_only_invariant.py`
fails if any of them comes back. `ops/dominus-nobara` is the retired Dominus
stack, kept only until it is decommissioned separately.
