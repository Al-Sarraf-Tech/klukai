# Dream image backend research and adoption gate

Research date: **2026-10-14**. This records public evidence and a proposed local evaluation; no paid image comparison or production activation occurred. Implementation scope is in the [provider specification](../superpowers/specs/2026-10-14-dream-image-providers.md).

## Recommendation

Prepare **GPT Image 2.5 Sunburst for quality**, **Flare for faster iteration**, and **Claude Sonnet 5.5 for dream/scene writing**. Keep ComfyUI as the default and local portrait/inpainting backend. Claude's role here is text composition, not an image-generation endpoint.

Pin `gpt-image-2.5-sunburst-2026-09-08` or `gpt-image-2.5-flare-2026-09-08`, explicit quality, dimensions, and reference bytes. Sunburst is the strongest quality candidate, but adoption depends on Klukai-specific identity tests. Confidence is high for broad quality leadership, moderate for anime suitability, and low for any claim of proven identity continuity across dream sequences.

## Independent quality evidence

Artificial Analysis (AA), retrieved October 14, evaluates blind human preferences. Values below are Elo ±95% interval, followed by sample count. Models are the exact displayed versions; no score is transferred to another quality setting.

| Model | Text to image | Editing |
| --- | ---: | ---: |
| GPT Image 2.5 Sunburst (max) | 1199±9; 14,974 | 1186±7; 19,569 |
| GPT Image 2.5 Flare (max) | 1192±9; 14,370 | 1167±7; 20,536 |
| Nano Banana 2.1 | 1159±10; 6,222 | 1141±9; 6,393 |
| Grok Imagine Image 2.0 | 1155±10; 8,071 | 1113±8; 9,445 |
| MAI-Image-2.6 | 1152±9; 10,441 | 1143±7; 17,614 |

Sunburst/Flare overlap on text-to-image, with rank ranges 1–2. Editing separates them more clearly. Sources: [AA generation](https://artificialanalysis.ai/image/leaderboard/text-to-image), [AA editing](https://artificialanalysis.ai/image/leaderboard/editing).

Arena independently supports the recommendation. Scores below retain the displayed uncertainty and per-model vote count; all Sunburst/Flare entries remain **Preliminary**.

| Arena board/date | Sunburst | Flare | Grok Image 2.0 |
| --- | ---: | ---: | --- |
| Overall generation, Oct 7 | 1425±7; 17,752 | 1398±7; 16,889 | Canvas: 1334±12; 3,113. `20260801`: 1294±6; 14,483 |
| Cartoon, Anime & Fantasy, Oct 7 | 1446±10; 6,601 | 1421±10; 6,424 | Canvas: 1372±18; 1,274. `20260801`: 1309±9; 5,198 |
| Single-image edit, Oct 6 | 1524±5; 59,825 | 1481±5; 57,408 | Canvas: 1439±9; 11,589. `20260801`: 1425±5; 50,427 |
| Multi-image edit, Oct 6 | 1527±6; 28,479 | 1476±6; 28,778 | No Grok entry on retrieved board |

Sources: [overall](https://arena.ai/leaderboard/image/text-to-image), [anime category](https://arena.ai/leaderboard/text-to-image/cartoon), [single-image](https://arena.ai/leaderboard/image/image-edit/single-image), [multi-image](https://arena.ai/leaderboard/image/image-edit/multi-image). The legacy anime URL successfully redirected to the current page; directly fetching its canonical URL intermittently failed.

Do not equate Grok Canvas with its dated API entry or compare Elo numbers across evaluators. Nano Banana 2.1 is a material alternative for multi-reference work: fourth in Arena multi-image editing, 1431±6 with 10,328 votes. Neither it nor MAI displaces Sunburst in these measurements.

## Cost and latency

Claude does not natively generate photos or illustrations; it can write prompts
and inspect results. [Anthropic's capability statement](https://support.claude.com/en/articles/9002504-can-claude-produce-images).
OpenAI edits accept up to 16 references and masks; these features do not import
Klukai's local LoRA or promise deterministic animation frames.
[Images API](https://developers.openai.com/api/reference/resources/images/methods/edit).

Grok's current renderer is `grok-imagine-image-2.0`, with up to five references.
At medium quality, output costs $0.06 at 1K, $0.07 at 1.5K, or $0.08 at 2K,
plus $0.01 per input image. One 2K image with one reference is therefore $0.09;
100 successful attempts cost $9 before retries or optional storage. Pin quality:
`auto` currently differs between generation and editing. Its REST schema has
no documented mask or deterministic seed control.
[Pricing](https://docs.x.ai/developers/pricing),
[multi-reference editing](https://docs.x.ai/developers/model-capabilities/images/multi-image-editing),
[REST schema](https://docs.x.ai/developers/rest-api-reference/inference/images.md).
Avoid the old Pro/Quality aliases: Quality retires November 2, 2026 and then
redirects to Imagine 2.0 at low quality.
[Migration notice](https://docs.x.ai/developers/migration/imagine-image-quality-nov-2).

AA's first-party OpenAI chart data, retrieved October 14, reports:

| Configuration | Benchmark price/image | Median latency | 5th–95th percentile |
| --- | ---: | ---: | ---: |
| Sunburst (max) | $0.21072 | 124.28 s | 96.07–188.88 s |
| Flare (max) | $0.21072 | 50.30 s | 44.92–65.33 s |

These are benchmark configurations, not universal per-image tariffs. Values came from embedded public chart JSON on the [Sunburst page](https://artificialanalysis.ai/image/models/gpt-image-2.5-sunburst-2026-09-08); [Flare page](https://artificialanalysis.ai/image/models/gpt-image-2.5-flare-2026-09-08). AA defines latency as a trailing three-day median including output download. Quality, dimensions, reference inputs, and token usage affect actual API cost. Request caps cannot guarantee a dollar ceiling.

OpenAI's Standard rates for both 2.5 variants are $5/M text input tokens,
$8/M image input tokens and $30/M image output tokens. Equal rates do not mean
equal cost per image; measure the returned usage. A 100-image planning figure
at AA's settings is about $21.07, with reference-input and prompt costs added.
[Official Sunburst pricing](https://developers.openai.com/api/docs/models/gpt-image-2.5-sunburst),
[image cost guidance](https://developers.openai.com/api/docs/guides/image-generation).

## What remains unproven

AA refreshes prompts monthly and includes Animation & Gaming and identity-preserving edits. However, its editing benchmark concerns localized changes; its separate reference-to-image benchmark is still described as upcoming. Arena's anime category also includes cartoons and fantasy. Neither establishes Klukai's face, outfit, or multi-character continuity. [AA methodology](https://artificialanalysis.ai/methodology/image).

ArXiv searches and full-paper sections confirmed the evaluation gap. [AnimeAdapter](https://arxiv.org/abs/2605.20237), May 17, 2026, retrieved v2, studies pose, expression, viewpoint, and scene changes, but compares older adapters; its external cartoon subset has nine identities. [DSH-Bench](https://arxiv.org/abs/2603.08090), March 9, 2026, retrieved v3, separates preservation, prompt following, and quality, but focuses on single subjects. Neither compares today's Sunburst and Grok versions. These papers inform the rubric, not the vendor ranking.

Research used native web, public HTTP chart data, and arXiv. Brave/search-mcp/context7 were unavailable; CUA had no browsers. Grok/X was not invoked under the spending restriction. No paid generation, CAPTCHA service, or research API was used.

## Privacy and authorization

[ADR-0006](../adr/0006-image-gen-pipeline.md) records why Commander-only content previously excluded cloud rendering. Cloud opt-in sends scene text and reference images outside the local stack; adopting a provider must explicitly resolve that concern.

OpenAI API data is not used for training by default; image generation/editing normally has 30-day abuse-monitoring retention and no application-state retention. Zero Data Retention has eligibility and image-specific limitations. [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data). xAI documents no API training without permission, 30-day audit retention, and ZDR limitations for stateful features. [xAI security](https://docs.x.ai/developers/faq/security). Anthropic's commercial defaults likewise exclude training and generally retain API inputs/outputs for 30 days, with documented exceptions. [Training](https://privacy.claude.com/en/articles/7996868-is-my-data-used-for-model-training), [retention](https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data). None of these defaults means private material never leaves the machine or is instantly deleted.

Codex's included image tool uses GPT Image 2 under Codex limits; its documentation directs programmatic integrations to the Images API. A subscription is not proof of prepaid GPT Image 2.5 API access. [Codex image generation](https://learn.chatgpt.com/docs/image-generation). The September 28 [Sign in with ChatGPT](https://developers.openai.com/cookbook/articles/sign-in-with-chatgpt) launch covers eligible local personal/open-source apps, while remotely hosted apps require approval; it does not establish entitlement for this backend.

The existing paid exception covers interactive Sonnet only. Image rendering and background creative text remain separately disabled, with zero request caps. This document authorizes no spending, activation, or upload.

## Fixed adoption comparison

The [12-scene manifest](2026-10-14-dream-image-evaluation.json) is a proposal, not an executable job. Before an authorized comparison, approve local reference files, record SHA256 digests, and freeze one pack for every provider: Klukai face/full-body, a synthetic Commander reference, and an edit source. Preserve the approved wardrobe and reference-side tear mark/ornament. No reference substitution, provider-specific prompt polishing, or retry-based cherry-picking.

Compare eligible existing local outputs and authorized candidates at recorded settings. Mask provider names, randomize order, and score every attempt, including failures. Grade visible face identity, green eyes, tear mark, silver hair, cross ornament, wardrobe, anatomy/pose, instruction fidelity, character separation, and preservation of untargeted content: 0 wrong/missing, 1 partial, 2 correct; genuinely occluded details are N/A. Record identity separately from overall appeal, latency, and cost. Identity swaps, fused characters, or lost earlier edits are explicit failures. The owner selects adoption after reviewing the evidence; a high average cannot erase an identity failure.
