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
  var current = null; // the line playing now: {url, resolve, token}
  var playToken = 0;

  function element() {
    if (!el) {
      el = new Audio();
      el.preload = 'auto';
      el.setAttribute('playsinline', '');
      // Only the clip that armed this token may finish. A synchronous
      // ended/error from the previous src (fired while the next clip is
      // assigned) used to resolve the new clip, so one bubble's playback
      // attached to the next message.
      el.addEventListener('ended', function () { finish(true, true); });
      el.addEventListener('error', function () { finish(false, true); });
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

  function finish(ok, fromMedia) {
    var item = current;
    var a = el;
    if (!item || !a || item.token !== a._playToken) return;
    // Media events from the previous src can arrive after this clip is armed.
    // They do not count until play() has accepted this url.
    if (fromMedia && !item.live) return;
    current = null;
    item.resolve(ok);
    var nextItem = queue.shift();
    if (nextItem) start(nextItem);
  }

  function start(item) {
    var token = ++playToken;
    item.token = token;
    item.live = false;
    current = item;
    var a = element();
    // Disarm across the src swap so a synchronous ended/error cannot resolve
    // this clip. A queued event still sees live === false and is ignored.
    a._playToken = 0;
    a.src = item.url;
    a._playToken = token;
    var p;
    try { p = a.play(); } catch (e) { p = Promise.reject(e); }
    Promise.resolve(p).then(function () {
      if (current === item && item.token === a._playToken) item.live = true;
    }, function () {
      if (current === item && item.token === a._playToken) finish(false);
    });
  }

  // iOS pauses the element in the background and never fires 'ended'. The
  // current clip would then block every later bubble until reload.
  function resumePaused() {
    if (typeof document !== 'undefined' && document.hidden) return;
    var item = current;
    var a = el;
    if (!item || !a || item.token !== a._playToken) return;
    if (!a.paused || a.ended) return;
    var p;
    try { p = a.play(); } catch (e) { p = Promise.reject(e); }
    Promise.resolve(p).then(null, function () {
      if (current === item && item.token === a._playToken) finish(false);
    });
  }

  GESTURES.forEach(function (t) {
    window.addEventListener(t, unlock, { capture: true, passive: true });
  });
  document.addEventListener('visibilitychange', resumePaused);
  window.addEventListener('pageshow', resumePaused);

  window.klukaiVoice = {
    /** Queue a line; resolves true when it has finished playing, false if the
     *  browser refused it (no gesture yet) or it failed to decode. */
    play: function (url) {
      preferPlayback();
      return new Promise(function (resolve) {
        queue.push({ url: url, resolve: resolve });
        if (!current) start(queue.shift());
        else resumePaused(); // a paused earlier clip must not pin this one
      });
    },
    isUnlocked: function () { return unlocked; },
  };
})();
