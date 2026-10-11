/**
 * Audio recorder bridge for Flutter Web — records mic input via MediaRecorder.
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
  // must not hold the mic open forever.
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
    if (stream) stream.getTracks().forEach(t => t.stop());
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
      rec.onstop = () => {
        const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' });
        const reader = new FileReader();
        reader.onloadend = () => {
          // Strip the data URL prefix; a take with no audio data is null, not "".
          resolve(reader.result.split(',')[1] || null);
        };
        reader.readAsDataURL(blob);
        releaseTracks(rec.stream);
      };
      rec.stop();
    });
  }

  // Abandon whatever take is in progress or waiting to be collected.
  function discard() {
    cappedClip = null;
    const rec = mediaRecorder;
    mediaRecorder = null;
    clearCap();
    if (!rec) return;
    if (rec.state !== 'inactive') rec.stop();
    releaseTracks(rec.stream);
  }

  window.audioRecorder = {
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
