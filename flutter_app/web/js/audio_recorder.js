/**
 * Audio recorder bridge for Flutter Web — records mic input via MediaRecorder.
 */
(function() {
  'use strict';

  let mediaRecorder = null;
  let audioChunks = [];

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

  window.audioRecorder = {
    async start() {
      let stream = null;
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        audioChunks = [];
        const mimeType = pickMimeType();
        mediaRecorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
        mediaRecorder.ondataavailable = (e) => {
          if (e.data.size > 0) audioChunks.push(e.data);
        };
        mediaRecorder.start();
        return true;
      } catch (e) {
        console.error('[audio_recorder] Failed to start:', e);
        // Release the mic, or Safari keeps the recording indicator lit.
        if (stream) stream.getTracks().forEach(t => t.stop());
        mediaRecorder = null;
        return false;
      }
    },

    stop() {
      return new Promise((resolve) => {
        if (!mediaRecorder || mediaRecorder.state === 'inactive') {
          resolve(null);
          return;
        }
        mediaRecorder.onstop = async () => {
          const blob = new Blob(audioChunks, { type: mediaRecorder.mimeType || 'audio/webm' });
          const reader = new FileReader();
          reader.onloadend = () => {
            // Strip data URL prefix to get pure base64
            const base64 = reader.result.split(',')[1];
            resolve(base64);
          };
          reader.readAsDataURL(blob);
          // Stop all tracks
          mediaRecorder.stream.getTracks().forEach(t => t.stop());
          mediaRecorder = null;
        };
        mediaRecorder.stop();
      });
    },

    isRecording() {
      return mediaRecorder !== null && mediaRecorder.state === 'recording';
    },
  };

  console.log('[audio_recorder] Ready');
})();
