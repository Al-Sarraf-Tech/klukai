# Ordered image backups: research and samples

Observed October 15, 2026 Central (October 16 UTC). This records development
probes. Production image selection and deployment are unchanged.

## Requested behavior

The owner requested the existing alternating Agy/Grok account pair as mainline,
Dominus GPU as the third choice, and Amarillo GPU last. Necessary free model and
runtime acquisition and actual Klukai samples were authorized. This does not
authorize paid image APIs, account extra usage, new credits or a voice fallback.
Preserve policy-refusal precedence, durable attempt ownership, ambiguous-request
recovery, private references, gaming checks, GPU leases and cleanup.

Production still selects `KLUKAI_IMAGE_PROVIDER=agy`; the account worker reports
providers `agy,grok` with alternating preference. No automatic GPU image chain
has been deployed. Voice and text provider policies remain separate.

## Actual samples

| Executor | Result | Limits |
| --- | --- | --- |
| Dominus GPU | One 832×1216 PNG, SHA256 `68a839becd25d985e7cba78367f4b04899c583a34b6e3331d0f40945415c0541`; renderer free/release returned success and the lease was inactive afterward | Tear mark appeared on the opposite side. The observer wrapper raised an old-client `close()` error after saving; the valid image and renderer cleanup are retained separately. |
| Amarillo Arc A380 | One 512×512 reference-guided PNG, SHA256 `19e7e2f558db57ecfa0a914c0a89481a58489ee5f66424c2001d7a69a80d03b3`; native execution 35.018s, terminal exit 0, stderr empty | Recognizable approximation, with missing tear, weak facial detail and altered cap, clips and equipment. Independent review does not consider it adequate canonical production artwork. |

The Amarillo result used BK-SDM Tiny revision
`0364108e53b7f7f4d2585e817a0b7a83dc261cfa`, converted privately from verified local
FP16 safetensors using CPU-only tools. The frozen 23-file OpenVINO export is
1,065,781,457 bytes. Its UNet floating constants are FP16. Source notices,
provenance, tokenizer parity checks and independent artifact hashes are retained.
Existing Granite and voice environments were unchanged.

The actual render used the installed OpenVINO/GenAI 2026.1 runtime, CPU text/VAE
and Arc denoising, explicit 512px reshape, CFG 7.5, PNDM 50 steps, strength 0.4,
and seed 416202610. The input was the tracked Klukai portrait, composited onto a
graphite background without cropping or mirroring. It is not the Dominus NoobAI
model or its LoRA, and does not demonstrate equivalent identity or new-scene
fidelity.

The worker ran without network or account mounts in a private namespace. The
existing 2GiB sampled own-GPU-memory, 70°C CPU and 360-second limits remained.
Observed local GPU residency peaked at 846,983,168 bytes and CPU temperature at
53°C. All owned process sessions ended; the frozen model remained unchanged and
the existing Granite PID/start identity and health were preserved. Global GPU
capacity/health telemetry remains unreliable, so these observations do not
certify production resource admission.

Earlier attempts are retained: an incompatible optional runtime setting failed
before generation; the larger LCM model exceeded the same memory guard; a
256px/CFG1 BK-SDM sample ran but lost facial features. The 512px case was a changed
resolution/guidance comparison, not a hidden identical-request retry. A separate
Amarillo account-worker request failed after one Agy attempt; it produced no GPU
image and does not prove a Grok outage.

## Remaining work

Review the responsive UI design and the durable provider-chain design, implement
and test both, then bind them to exact-source CI and verified deployment. The
Amarillo model still needs a credible character-fidelity solution before being
presented as a canonical artwork provider. Preserve the current mainline while
these requirements remain open.

The owner also requested Dominus shutdown. Its graceful RabbitMQ action was
acknowledged; after earlier on-state observations, the controller confirmed off
at 22:14:31 CDT on October 15. No hard power cut was sent. Remote jobs remain
held. Do not resume GPU/heavy work there or power it on without checking the
owner's latest intent and the existing host schedule.

Private raw evidence is under `~/.local/state/klukai/s-uplift-20261015/`, including
`gpu-samples-20261016T021100Z`, `amarillo-bksdm-export20261016` and
`amarillo-bksdm-gpu-20261016T030001Z`. These are development samples and do not
certify the full S-tier ledger, GitHub CI or a new deployment.
