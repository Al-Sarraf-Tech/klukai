# Klukai voice reference

The owner-selected voice baseline is the complete Japanese recording from
[Klukai Voice Line [Girls' Frontline 2: Exilium]](https://www.youtube.com/watch?v=3tJ1ZE11J34)
by HollowMelody, selected on 2026-10-13.

The baseline artifacts are:

- [Source clip](baseline/3tJ1ZE11J34.mp4): the complete 720p video with its
  original H.264 video and AAC audio streams, remuxed without re-encoding.
- [Baseline audio](baseline/3tJ1ZE11J34.wav): the complete audio decoded to
  22,050 Hz, mono, 16-bit PCM, matching the repository's reference WAV format.
  No speech was cut, denoised, normalized, or pitch-shifted.
- [Provenance](baseline/source.json): source URL, uploader, dates, media
  properties, extraction details, and SHA-256 digests.
- [Checksums](baseline/SHA256SUMS): verify with
  `cd docker/voice/reference/baseline && sha256sum -c SHA256SUMS`.

Use this recording as the source baseline for listening, voice comparison,
and selecting short, speech-only conditioning segments. The full compilation
also preserves pauses and any game audio present in the source.

The deployed XTTS conditioning artifact is `klukai_reference.wav`, pinned in
`ops/dominus-nobara/models.lock.json`. This baseline archival commit does not
deploy a replacement. Runtime adoption requires a curated conditioning clip,
an updated artifact lock, and deployment through the canonical Dominus stack;
see [ADR 0007](../../../docs/adr/0007-voice-on-dominus.md).
