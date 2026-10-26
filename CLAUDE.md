# Klukai

**Cloud AI only (owner decision, 2026-10-26).** Klukai runs no inference on
Dominus-Nobara or Amarillo: chat is Claude Sonnet 5.5, every background model
call and memory recall is Claude Haiku 5.5 (high effort), voice in and out is
Fish Audio, pictures use the cloud providers. There is no local fallback of
any kind. Design: `docs/superpowers/specs/2026-10-26-cloud-only-ai-design.md`.
Do not reintroduce LM Studio, Dominus voice, ComfyUI, the GPU lease, local
embeddings or Qdrant into `docker/core`; `tests/test_cloud_only_invariant.py`
fails if any of them comes back. `ops/dominus-nobara` is the retired Dominus
stack, kept only until it is decommissioned separately.
