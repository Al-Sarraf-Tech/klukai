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
  let starting = null;      // in-flight start(): stop() waits for it instead of racing it
  let stopRequested = false; // stop() arrived while start() was still waiting for the mic
  let safetyTimer = null;
  let capped = null;         // result of an auto-stopped recording, handed to the next stop()

  // A stuck gesture (pointer left the button, long-press menu) would otherwise
  // keep the mic hot indefinitely.
  const MAX_RECORDING_MS = 60000;

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
  function releaseStream(stream) {
    try { if (stream) stream.getTracks().forEach(t => t.stop()); } catch (_) { /* already gone */ }
  }

  function clearSafety() {
    if (safetyTimer) { clearTimeout(safetyTimer); safetyTimer = null; }
  }

  // Forget a recorder: release its mic and, if it is still the active one, drop
  // it and its safety timer (a stale onstop must not disarm a newer recorder's).
  function finish(rec) {
    releaseStream(rec && rec.stream);
    if (mediaRecorder === rec) { mediaRecorder = null; clearSafety(); }
  }

  async function begin() {
    let stream = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (stopRequested) {
        // The gesture ended before the mic arrived: nothing to record, and
        // nobody would stop a recorder started now.
        releaseStream(stream);
        return false;
      }
      audioChunks = [];
      const mimeType = pickMimeType();
      const rec = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
      rec.ondataavailable = (e) => {
        if (e.data.size > 0) audioChunks.push(e.data);
      };
      // A recorder that errors goes inactive without onstop; release the mic
      // now so the later stop() finds nothing and resolves null.
      rec.onerror = (e) => {
        console.error('[audio_recorder] Recorder error:', (e && e.error) || e);
        finish(rec);
      };
      rec.start();
      mediaRecorder = rec;
      clearSafety();
      safetyTimer = setTimeout(() => {
        safetyTimer = null;
        if (mediaRecorder !== rec) return;
        console.warn('[audio_recorder] 60 s cap reached; releasing the mic');
        capped = window.audioRecorder.stop(); // what was captured still reaches the next stop()
      }, MAX_RECORDING_MS);
      return true;
    } catch (e) {
      console.error('[audio_recorder] Failed to start:', e);
      releaseStream(stream);
      mediaRecorder = null;
      return false;
    }
  }

  window.audioRecorder = {
    /** Resolves true when recording started, false otherwise. Never rejects. */
    start() {
      if (starting) return starting; // a start is already in flight
      if (mediaRecorder && mediaRecorder.state === 'recording') return Promise.resolve(true);
      stopRequested = false;
      capped = null;
      starting = begin().finally(() => { starting = null; });
      return starting;
    },

    /** Resolves the base64 audio, or null when nothing was recorded. Never rejects. */
    async stop() {
      if (starting) {
        // Called while start() still waits for getUserMedia: wait for it, and
        // tell it not to start a recorder nobody would stop.
        stopRequested = true;
        try { await starting; } catch (_) { /* begin() never rejects */ }
        stopRequested = false;
      }
      clearSafety();
      const rec = mediaRecorder;
      if (!rec || rec.state === 'inactive') {
        mediaRecorder = null;
        if (capped) { const c = capped; capped = null; return c; }
        return null;
      }
      return new Promise((resolve) => {
        let settled = false;
        const settle = (v) => { if (!settled) { settled = true; resolve(v); } };
        rec.onstop = () => {
          finish(rec); // stop all tracks first: the mic goes dark immediately
          const blob = new Blob(audioChunks, { type: rec.mimeType || 'audio/webm' });
          const reader = new FileReader();
          reader.onloadend = () => {
            // Strip data URL prefix to get pure base64
            const b64 = typeof reader.result === 'string' ? reader.result.split(',')[1] : null;
            settle(b64 || null);
          };
          reader.onerror = () => settle(null);
          reader.readAsDataURL(blob);
        };
        rec.onerror = (e) => {
          console.error('[audio_recorder] Recorder error:', (e && e.error) || e);
          finish(rec);
          settle(null);
        };
        try {
          rec.stop();
        } catch (e) {
          console.error('[audio_recorder] Failed to stop:', e);
          finish(rec);
          settle(null);
        }
      });
    },

    isRecording() {
      return mediaRecorder !== null && mediaRecorder.state === 'recording';
    },
  };

  console.log('[audio_recorder] Ready');
})();
