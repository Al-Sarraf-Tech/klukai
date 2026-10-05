// Her voice on iOS Safari, desktop Safari and Brave.
//
// iOS only lets a media element play once a user gesture has "blessed" it. A
// fresh `new Audio()` created when a voice frame arrives over the WebSocket is
// not inside a gesture, so it is refused (NotAllowedError) and she is silent.
// Fix: ONE shared <audio> element, blessed on the first touch/click/key, and
// reused for every line she speaks. Lines are queued, so two frames that
// arrive close together play one after the other instead of on top of each
// other.
//
// navigator.audioSession.type = 'playback' (Safari 16.4+) asks iOS to treat
// this as media playback, which keeps playing with the ring/silent switch on.
// Older iOS ignores it; nothing else changes.
(function () {
  'use strict';

  // 44-byte silent WAV: enough for play() to succeed and bless the element.
  var SILENT = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=';
  var GESTURES = ['touchend', 'pointerup', 'click', 'keydown'];

  var el = null;
  var unlocked = false;
  var queue = [];
  var current = null; // the line playing now: {url, resolve}

  function element() {
    if (!el) {
      el = new Audio();
      el.preload = 'auto';
      el.setAttribute('playsinline', '');
      el.addEventListener('ended', function () { finish(true); });
      el.addEventListener('error', function () { finish(false); });
    }
    return el;
  }

  function preferPlayback() {
    try {
      if (navigator.audioSession) navigator.audioSession.type = 'playback';
    } catch (e) { /* not supported */ }
  }

  function unlock() {
    if (unlocked) return;
    preferPlayback();
    if (current) { unlocked = true; return; } // already playing: already allowed
    var a = element();
    a.src = SILENT;
    var p;
    try { p = a.play(); } catch (e) { return; }
    var done = function () {
      unlocked = true;
      GESTURES.forEach(function (t) { window.removeEventListener(t, unlock, true); });
    };
    if (p && typeof p.then === 'function') {
      p.then(function () { if (!current) a.pause(); done(); }, function () { /* next gesture */ });
    } else {
      done();
    }
  }

  function finish(ok) {
    var item = current;
    current = null;
    if (item) item.resolve(ok);
    var nextItem = queue.shift();
    if (nextItem) start(nextItem);
  }

  function start(item) {
    current = item;
    var a = element();
    a.src = item.url;
    var p;
    try { p = a.play(); } catch (e) { p = Promise.reject(e); }
    Promise.resolve(p).then(null, function () {
      if (current === item) finish(false); // refused (no gesture yet) or undecodable
    });
  }

  GESTURES.forEach(function (t) {
    window.addEventListener(t, unlock, { capture: true, passive: true });
  });

  window.klukaiVoice = {
    /** Queue a line; resolves true when it has finished playing, false if the
     *  browser refused it (no gesture yet) or it failed to decode. */
    play: function (url) {
      preferPlayback();
      return new Promise(function (resolve) {
        queue.push({ url: url, resolve: resolve });
        if (!current) start(queue.shift());
      });
    },
    isUnlocked: function () { return unlocked; },
  };
})();
