# Production: Sonnet primary and Amarillo Arc voice

Deployed on Amarillo on 2026-10-14. Implementation commits: `44b3f68` and
`a3c5fda`. The owner requested production deployment and explicitly overrode
the no-spending rule for this existing Anthropic API integration after being
told it uses paid API billing. This exception does not cover unrelated costs.

## Live configuration

- Interactive authenticated chat and tool calls: `claude-sonnet-5-5`, adaptive
  thinking, `output_config.effort=medium`, official Anthropic Messages API.
- Dominus remains the secondary, including its gaming selection and existing
  leases/idle behavior. Background jobs remain local. Its gateway timed out
  from Amarillo during acceptance; no power-on or GPU-stack change was made.
- `VOICE_BACKEND=amarillo-xpu`, `VOICE_URL=http://100.111.198.19:8301`.
  `VOICE_STT_URL=http://100.107.121.5:8301`; transcription still needs Dominus.
- `klukai-voice-xpu.service` is enabled and active. The pinned XTTS v2 model
  loads lazily on the Arc A380 using FP16 and unloads after 300 idle seconds.
  Inference has a 600-second deadline, a 68°C guard, 8 GiB RAM cap, no swap,
  and two CPU threads. English text is supported; Japanese text is rejected
  before load because the tested environment has no Japanese tokenizer.
- Core image:
  `sha256:1715594e892fb6050f25b6973610332d283aad1dafdbd55ce48cd2cd205c90d2`.
- Flutter bundle `main.dart.f22cc76a.js`, bootstrap
  `flutter_bootstrap.7b007cea.js`. The stage uses versioned module URLs because
  the public CDN applies a four-hour browser cache to unhashed JavaScript.
  Refreshing its uncached HTML now selects `app.js?v=9297f315`,
  `net.js?v=ed5305f8`, and `voice.js?v=12e6e4bd`.

The API key is supplied through the ignored mode-0600 production `.env`.
The native service has only its voice token in the external mode-0600
`~/.config/klukai/voice-xpu.env`. No credentials are committed. The original
Dominus reference, model lock, containers, and startup repair are unchanged.

## Acceptance evidence

- Full core run: 4,646 passed, 58 integration/performance tests deselected.
  Existing coroutine/deprecation warnings remained. Follow-up queue and
  response-wrapper fixes passed their focused regressions (118 and 78 tests
  respectively); the exact HTTP200 wrapper failure was reproduced first.
- Companion JavaScript: 99 passed. Flutter Chrome: 290 passed; changed Dart
  analysis clean. Amarillo operations: 23 passed, including 20 native voice
  tests. Core image and Flutter release builds succeeded.
- Independent review found and verified the fix for cancelled queue entries
  retaining capacity. API/tool tests preserve signed thinking internally,
  prevent duplicate fallback output, and report the actual answering model.
- Authenticated production websocket returned `Ready, Commander.` with done
  model `claude-sonnet-5-5` in 15.41 seconds. A separate minimal API preflight
  verified adaptive thinking and medium effort are accepted by this model.
- Production TTS submitted as 202, polled to completion, and decoded to mono
  PCM16 WAV at 24,000 Hz: 1.5253 seconds of audio, 73,260 bytes. Cold wall time
  was 62.27 seconds. SHA-256:
  `7ced8ffddef4893c6d8287b555b1ec03646ccef30f3af13400e3839f62c832bd`.
  This proves synthesis/format, not a perceptual quality assessment. Arc voice
  remains substantially slower than real time.
- The next active job was cancelled successfully. Native health then showed
  `busy=false`, `tts_ready=false`, and no worker child remained. Authenticated
  explicit unload also succeeded. Idle release was observed before the rerun
  after the earlier successful native synthesis had been idle over five minutes.
- Public authenticated job polling returned `Cache-Control: no-store`,
  `CF-Cache-Status: DYNAMIC`. Unauthenticated core jobs and native TTS returned
  401. Public versioned modules and Flutter bundles match deployed bytes.
- CPU peak 44°C, Arc peak 51°C across acceptance sampling. Granite remained
  active with original PID 1799 throughout. No existing GPU service was stopped.

Runtime evidence and the audio sample are under
`/home/jalsarraf/Documents/Codex/2026-10-14/klukai-production-release/`:
`smoke-result.json`, `final-validation.json`, `production-voice.wav`, public
asset hashes, thermal samples, and rollback material. That directory is private
(0700); environment snapshots inside it are 0600 and must never be printed.

## Rollback

The prior image is tagged `localhost/klukai-core:before-sonnet-xpu-20261014`
(`sha256:0eb5870424a6dc1da597116762336b004513f4ed87c98cb5d6e57e562dbe6831`).
The release evidence directory contains `rollback.env` and `web-build-before/`.

Restore that environment and web snapshot, retag the prior image as
`companion-companion-core:latest`, and recreate only `companion-core` with
`docker compose up -d --no-deps --no-build companion-core`. Stop/disable only
`klukai-voice-xpu.service`; it reaps its own worker. Do not restart Docker,
Granite, or Dominus. Restoring the old voice route cannot make a powered-off
or unreachable Dominus available.
