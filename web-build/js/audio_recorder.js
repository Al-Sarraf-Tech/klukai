/**
 * Audio recorder bridge for Flutter Web — records mic input via MediaRecorder.
 *
 * Contract with chat_screen.dart (push-to-talk): start() resolves a boolean,
 * stop() resolves the base64 audio or null. Both must always settle — the
 * Dart side awaits them inside a gesture handler.
 */
(function() {
  'use strict';

  let mediaRecorder = null;
  let audioChunks = [];
  // Bumped by every start() and stop(). A start() that comes back from the
  // permission prompt with a stale generation was already stopped or
  // superseded and must release the mic instead of recording.
  let generation = 0;
  let capTimer = null;
  // The clip the cap already finished, waiting for the button release to collect it.
  let cappedClip = null;

  // The server caps STT at ~6 MB decoded, and a stuck or forgotten button
  // (pointer left the button, long-press menu) must not hold the mic open forever.
  const MAX_RECORDING_MS = 60_000;

  // Safari (iOS and macOS) has no WebM MediaRecorder: asking for
  // 'audio/webm' threw NotSupportedError, so push-to-talk was dead there.
  // Take the first container this browser can record; the STT service
  // sniffs the format, and Whisper decodes WebM/Opus and MP4/AAC alike.
  const PREFERRED_TYPES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/aac'];

  function pickMimeType() {
    if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return '';
    for (const t of PREFERRED_TYPES) {
      if (MediaRecorder.isTypeSupported(t)) return t;
    }
    return ''; // let the browser choose its default
  }

  // Release the mic, or Safari keeps the recording indicator lit.
  function releaseTracks(stream) {
    try { if (stream) stream.getTracks().forEach(t => t.stop()); } catch (_) { /* already gone */ }
  }

  function clearCap() {
    if (capTimer !== null) clearTimeout(capTimer);
    capTimer = null;
  }

  // Stop the take in progress, release the mic and resolve its base64 clip.
  function finish() {
    const rec = mediaRecorder;
    const chunks = audioChunks;
    mediaRecorder = null;
    clearCap();
    if (!rec) return Promise.resolve(null);
    if (rec.state === 'inactive') {
      releaseTracks(rec.stream);
      return Promise.resolve(null);
    }
    return new Promise((resolve) => {
      let settled = false;
      const settle = (v) => { if (!settled) { settled = true; resolve(v); } };
      rec.onstop = () => {
        releaseTracks(rec.stream); // the mic goes dark before the clip is encoded
        try {
          const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' });
          const reader = new FileReader();
          reader.onloadend = () => {
            // Strip the data URL prefix; a take with no audio data (or a failed
            // read, whose result is null) is null, not "".
            settle(String(reader.result || '').split(',')[1] || null);
          };
          reader.onerror = () => settle(null);
          reader.readAsDataURL(blob);
        } catch (e) {
          // onstop runs after stop() returns, outside its catch below.
          console.error('[audio_recorder] Failed to encode:', e);
          settle(null);
        }
      };
      // A recorder that errors goes inactive without onstop: settle the Dart
      // await instead of hanging it.
      rec.onerror = (e) => {
        console.error('[audio_recorder] Recorder error:', (e && e.error) || e);
        releaseTracks(rec.stream);
        settle(null);
      };
      try {
        rec.stop();
      } catch (e) {
        console.error('[audio_recorder] Failed to stop:', e);
        releaseTracks(rec.stream);
        settle(null);
      }
    });
  }

  // Abandon whatever take is in progress or waiting to be collected.
  function discard() {
    cappedClip = null;
    const rec = mediaRecorder;
    mediaRecorder = null;
    clearCap();
    if (!rec) return;
    try { if (rec.state !== 'inactive') rec.stop(); } catch (_) { /* already stopped */ }
    releaseTracks(rec.stream);
  }

  window.audioRecorder = {
    /** Resolves true when recording started, false otherwise. Never rejects. */
    async start() {
      const gen = ++generation;
      discard();
      let stream = null;
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        // A quick tap released the button before the mic was granted, or
        // another start() took over: nothing to record, let the mic go.
        if (gen !== generation) {
          releaseTracks(stream);
          return false;
        }
        // Each take keeps its own chunk list so a recorder still flushing
        // after stop() cannot spill into the next one.
        audioChunks = [];
        const chunks = audioChunks;
        const mimeType = pickMimeType();
        const rec = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
        rec.ondataavailable = (e) => {
          if (e.data.size > 0) chunks.push(e.data);
        };
        // An error before stop() releases the mic now; the later stop() then
        // finds an inactive recorder and resolves null.
        rec.onerror = (e) => {
          console.error('[audio_recorder] Recorder error:', (e && e.error) || e);
          releaseTracks(rec.stream);
        };
        rec.start();
        mediaRecorder = rec;
        capTimer = setTimeout(() => {
          capTimer = null;
          cappedClip = finish();
        }, MAX_RECORDING_MS);
        return true;
      } catch (e) {
        console.error('[audio_recorder] Failed to start:', e);
        releaseTracks(stream);
        return false;
      }
    },

    /** Resolves the base64 audio, or null when nothing was recorded. Never rejects. */
    stop() {
      // A start() still waiting on the permission prompt must not record after this.
      generation++;
      if (cappedClip) {
        // The cap already ended this take; hand the clip out once.
        const clip = cappedClip;
        cappedClip = null;
        return clip;
      }
      return finish();
    },

    isRecording() {
      return mediaRecorder !== null && mediaRecorder.state === 'recording';
    },
  };

  console.log('[audio_recorder] Ready');
})();
