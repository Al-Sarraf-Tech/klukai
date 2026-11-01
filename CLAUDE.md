# Klukai

**Cloud AI only (owner decision, 2026-10-26).** Klukai runs no inference on
Dominus-Nobara or Amarillo. Voice in and out is Fish Audio. There is no local
fallback of any kind. Design: `docs/superpowers/specs/2026-10-26-cloud-only-ai-design.md`.

**Chat is Claude Haiku 5.5 at xhigh (owner decision, 2026-10-31).** With
`KLUKAI_CLAUDE_PRIMARY=true` and `ANTHROPIC_API_KEY`, chat and the agent loop
use `claude-haiku-5-5`, `output_config.effort` `xhigh`, adaptive thinking.
A Haiku failure before any text falls back to Grok: the xAI API
(`grok-4.7`, prepaid `KLUKAI_XAI_API_KEY`, daily cap `KLUKAI_API_DAILY_USD_CAP`),
then the Grok CLI. Pictures use the xAI API.

**Background text is Claude Haiku 5.5 at low (owner decision, 2026-11-01).**
Under the same two settings, memory recall, extraction, promises, mood and the
other background writers use `claude-haiku-5-5` at effort `low`. A failure
goes to the Grok CLI on the owner's account login, then the caller's safe
default. Details: `AGENTS.md`. Fish voice is unchanged.
Do not reintroduce LM Studio, Dominus voice, ComfyUI, the GPU lease, local
embeddings or Qdrant into `docker/core`; `tests/test_cloud_only_invariant.py`
fails if any of them comes back. `ops/dominus-nobara` is the retired Dominus
stack, kept only until it is decommissioned separately.
