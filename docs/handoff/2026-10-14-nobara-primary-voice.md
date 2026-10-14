# Nobara primary speech output

On 2026-10-14 the owner requested main voice output on Dominus-Nobara's GPU,
with better speed and clarity. The selected route is the existing local XTTS
v2 clone on the RTX 3090. The existing Sonnet primary, Brave tools, image
provider, STT endpoint and credentials are independent of this change.

## Voice and playback

Core settings are `VOICE_BACKEND=dominus`,
`VOICE_URL=http://100.107.121.5:8301`, and `VOICE_ASYNC_JOBS=true`.
`VOICE_STT_URL` stays on Dominus. The new opt-in keeps `/api/tts` on the existing
authenticated 202 submit/poll/cancel interface. Each job remains limited to
500 characters; ownership checks, bounded admission and cancellation retain
their existing behavior. Arc always uses jobs; other Dominus deployments keep
the legacy synchronous default unless explicitly opted in.

The opt-in also gives microphone and archive synthesis the existing 600-second
deadline instead of the old 30/60-second HTTP limits. It does not bypass the
exclusive GPU lease, game marker or cleanup. The gateway still unloads XTTS
when each lease is released; repeated sentences can therefore incur cold-load
cost. Health probes do not load models.

Pinned Coqui 0.22's explicit-checkpoint loader initialized WAV output from
`audio.sample_rate` (22,050 Hz). XTTS actually generates samples at
`audio.output_sample_rate` (24,000 Hz). The service now sets the synthesizer's
output rate from that decoder setting. It neither resamples nor pitch-shifts
the waveform. The regression reproduces the old header error and proves
unchanged PCM samples with the correct rate and duration.

Dominus retains its original 14-second Klukai conditioning artifact, SHA-256
`276e9bb51b901fb941077130ed24c42e7af3e91c0255119b58eca7a602bc5a00`,
and XTTS revision `6c2b0d75eae4b7047358e3b6bd9325f857d43f77`.
The Arc release's 6.866-second reference is separately pinned and was not
copied over the Dominus artifact. No model/reference lock or dependency changed.

## Measurements and model choice

Both backends received: “Commander, this is Klukai. The connection is clear.
I am ready when you are.” These are single cold-request observations with each
backend's own pinned reference, not controlled model-quality benchmarks.

| Route | Full request time | Audio duration | WAV rate |
| --- | ---: | ---: | ---: |
| Previous Arc primary | 138.483 s | 6.827 s | 24,000 Hz |
| Original Nobara service | 13.644 s | 7.316 s | 22,050 Hz (incorrect) |
| Corrected Nobara service | 13.727 s | 8.578 s | 24,000 Hz |

The corrected voice process was observed using CUDA, peaking at 2,330 MiB.
After lease cleanup the model was unloaded; 344 MiB of CUDA context remained
in the service process. Game shutdown still stops that process. Output duration
varies because synthesis is stochastic. These measurements establish speed
and format, not a blind subjective voice-quality win.

Audio and measurement evidence are in
`/home/jalsarraf/Documents/Codex/2026-10-14/klukai-nobara-voice/` on Amarillo.

The initial comparison favored correcting and using the existing CUDA service
before changing the voice model. [Qwen3-TTS 0.6B Base](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-Base)
is a candidate for a separate English/Japanese cloning comparison, but has not
been benchmarked here. Its [Python wrapper](https://github.com/QwenLM/Qwen3-TTS/blob/main/qwen_tts/inference/qwen3_tts_model.py)
does not expose the advertised internal-engine streaming latency. A replacement
needs its own pinned environment and audio comparison; do not upgrade Torch or
Transformers inside the existing XTTS image. [Chatterbox Turbo](https://huggingface.co/ResembleAI/chatterbox-turbo)
is English-only, while [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) uses
preset voices and would change the selected identity. No new model was installed.

## Deployment and rollback

The voice image was built offline as one reviewed application-source layer on
the existing image, preserving all installed packages. Original image:
`sha256:6e26c59bf5c0be57413fe9cc58a8bc4a9d736dde34c1bbf687da4acdd285f801`.
Corrected image:
`sha256:f050c0cb429ee3bda690a05daa37623dc953a64ecb83e556382ee09d45d7f1ae`.
New `/app/app/main.py` SHA-256:
`a450211e7fb48fb753bb17a1e4ab7289b3ad73bf29f4918851fbd350c9b11ff3`.

On Dominus, build metadata, the prior source, GPU samples and the minimal image
recipe are in `/mnt/nvmer0/services/ai-stack/snapshots/voice-24000-20261014/`.
The canonical voice source was updated to match, and only `companion-voice`
was recreated through its canonical Compose file after verifying no game,
active lease or inference. The other five canonical container IDs and start
times were unchanged. No Docker daemon, llama-router or ComfyUI restart occurred.

The prior image is retained as `dominus-companion-voice:before-24000-20261014`;
the reviewed image is tagged `dominus-companion-voice:voice-24000-20261014`
and uses the canonical service tag for startup. A voice rollback restores
`main.before.py` from that snapshot and retags the prior image to
`dominus-companion-voice:2026-08-01`, then recreates only the voice service while
the same guards permit it. The full canonical source snapshot remains older;
this was a bounded voice-source update, not a fleet source-tree promotion.

Arc remains installed as an explicit rollback option. Set its matching
backend/URL and recreate only core to select it. Neither route is an automatic
failover to the other host. Nobara must be online and outside game mode for its
voice synthesis to be available.

Predeployment verification: 4,877 core tests passed (57 skipped, one performance
test deselected; nine existing warnings), 33 voice tests passed, and independent
review found no remaining issue after the microphone/archive timeout follow-up.
