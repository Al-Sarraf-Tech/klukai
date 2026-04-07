/**
 * Audio recorder bridge for Flutter Web — records mic input via MediaRecorder.
 */
(function() {
  'use strict';

  let mediaRecorder = null;
  let audioChunks = [];

  window.audioRecorder = {
    async start() {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        audioChunks = [];
        mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });
        mediaRecorder.ondataavailable = (e) => {
          if (e.data.size > 0) audioChunks.push(e.data);
        };
        mediaRecorder.start();
        return true;
      } catch (e) {
        console.error('[audio_recorder] Failed to start:', e);
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
          const blob = new Blob(audioChunks, { type: 'audio/webm' });
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
