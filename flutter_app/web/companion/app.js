// Companion stage — wiring. Live portrait (default) or 3D window, her voice,
// a generative score, and a chat dock, all on the same session as the PWA
// (messages sent here appear there too: the server fans out to every device).

import { readToken, clearToken, createApi, AuthExpired, wsUrl, createSocket } from "./net.js?v=ed5305f8";
import { moodToExpression, sceneFor, reduceFrame, abandonTurn, avatarCue } from "./logic.js";
import { createPortrait } from "./portrait.js";
import { createAmbience, ambienceKind } from "./ambience.js";
import { createMusic } from "./music.js";
import { createVoice } from "./voice.js?v=12e6e4bd";
import { createStageAppearance } from "./stage_appearance.js";
import {
  applySubtitle, completedAnnouncement, clearDecorativeMotion, motionPreference,
  beginWake, portraitStatusNotice,
} from "./stage_life.js";

const $ = (id) => document.getElementById(id);
const PREFS_KEY = "klukai_companion_prefs";
const DAY_REFRESH_MS = 5 * 60 * 1000;
const PORTRAIT_POLL_MS = 15000;
const FRAME_ERROR_REFRESH_MS = 500;    // several frames 403 at once: one refresh for all of them
const FRAME_ERROR_COOLDOWN_MS = 15000; // and if the fresh URLs fail too, don't loop on it
const TINTS = {
  base: "rgba(79, 195, 247, 0.10)", smile: "rgba(232, 140, 165, 0.14)",
  blush: "rgba(232, 140, 165, 0.20)", annoyed: "rgba(232, 146, 62, 0.14)",
};

function loadPrefs() {
  try {
    const prefs = { mode: "portrait", appearance: "default", music: true, voice: true, ...JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") };
    // Migrate before prefetch/wake: a retired model's 404 must not turn a
    // saved 3D preference into portrait mode while the catalog is loading.
    if (prefs.appearance === "hk416_gfl64") {
      prefs.appearance = "default";
      savePrefs(prefs);
    }
    return prefs;
  }
  catch { return { mode: "portrait", appearance: "default", music: true, voice: true }; }
}
function savePrefs(p) {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(p)); } catch { /* private mode */ }
}

function boot() {
  const token = readToken();
  if (!token) { location.replace("/"); return; }
  const toLogin = () => { clearToken(); location.replace("/"); };

  const stage = $("stage");
  const prefs = loadPrefs();
  const api = createApi({ token, onAuthExpired: toLogin });
  const portrait = createPortrait($("portrait"), { onFrameError: onPortraitFrameError });
  const ambience = createAmbience($("ambience"));
  let state = { line: "", mood: "composed", thinking: false, streaming: false };
  let day = null;
  let music = null;
  let voice = null;
  let avatar = null;
  let portraitTimer = null;
  let portraitSeq = 0;           // refreshPortrait() calls in flight: only the newest may apply
  let portraitHasFrames = false; // she has real frames showing (keep them through a blip)
  let frameErrorTimer = null;
  let lastFrameErrorRefresh = 0;
  let noticeTimer = null;
  let awake = false;

  const notice = (text, ms = 4000) => {
    const n = $("notice");
    n.textContent = text;
    n.hidden = !text;
    clearTimeout(noticeTimer);
    if (text && ms) noticeTimer = setTimeout(() => { n.hidden = true; }, ms);
  };

  function renderLine({ jumpToLatest = false } = {}) {
    const sub = $("subtitle");
    const placed = applySubtitle(sub, state.line, { jumpToLatest });
    sub.classList.toggle("thinking", state.thinking);
    const latest = $("btn-latest");
    if (latest) latest.hidden = placed.followed;
  }

  function announceCompleted(frame) {
    const text = completedAnnouncement(frame);
    const live = $("reply-live");
    if (text == null || !live) return;
    live.textContent = text;
  }

  function reducedNow() {
    return motionPreference(globalThis.matchMedia?.("(prefers-reduced-motion: reduce)"));
  }

  function applyMotionPreference() {
    const reduced = reducedNow();
    document.documentElement.dataset.reducedMotion = reduced ? "reduce" : "no-preference";
    if (reduced) clearDecorativeMotion(document);
  }

  function coverForWake(covered) {
    for (const id of ["portrait", "model"]) {
      const el = $(id);
      if (!el) continue;
      if (covered) el.setAttribute("inert", "");
      else el.removeAttribute("inert");
    }
    for (const sel of [".topbar", ".dock"]) {
      const el = document.querySelector(sel);
      if (!el) continue;
      if (covered) el.setAttribute("inert", "");
      else el.removeAttribute("inert");
    }
  }

  function applyMood() {
    const expr = moodToExpression(state.mood);
    portrait.setExpression(expr);
    stage.style.setProperty("--tint", TINTS[expr] || TINTS.base);
    avatar?.setTint?.(expr); // the viewer's own rim colour for this expression
    // While she thinks the pose stays; settle() hands her the mood when it ends.
    if (!state.thinking) avatar?.setMood(state.mood);
    applyScene();
  }

  // The 3D canvas runs under the top bar and the dock (companion.css fades
  // its edges); tell the viewer how much of it they cover so it frames her
  // in the visible band. Not re-run when the subtitle grows: she'd jump.
  function syncInsets() {
    const box = $("model").getBoundingClientRect();
    if (!avatar?.setInsets || !box.height) return;
    const top = Math.max(0, document.querySelector(".topbar").getBoundingClientRect().bottom - box.top);
    const bottom = Math.max(0, box.bottom - document.querySelector(".dock").getBoundingClientRect().top);
    avatar.setInsets({ top: Math.round(top), bottom: Math.round(bottom) });
  }

  // State changed (a frame, or the link dropped): repaint and keep the 3D
  // pose in step. The thinking pose is left explicitly — a mood frame isn't
  // guaranteed to follow, and it usually carries the same mood.
  function settle(prev) {
    renderLine();
    if (state.mood !== prev.mood) applyMood();
    const cue = avatarCue(prev, state);
    if (cue === "think") avatar?.setState("thinking");
    else if (cue === "release") avatar?.setMood(state.mood);
  }

  function applyScene() {
    const sc = sceneFor({ status: day?.status, hour: new Date().getHours(), weather: day?.weather, mood: state.mood });
    music?.setScene(sc);
    ambience.setKind(ambienceKind(sc));
  }

  async function refreshDay() {
    try {
      day = await api.herDay();
      $("status-line").textContent = day.status?.label || "The Elmo";
      const chip = $("outfit-chip");
      chip.textContent = day.outfit?.name || "";
      chip.hidden = !day.outfit?.name;
      applyScene();
    } catch { /* fail-soft: keep the last status */ }
  }

  async function refreshPortrait() {
    const seq = ++portraitSeq;
    clearTimeout(portraitTimer);
    portraitTimer = null;
    let p;
    try {
      p = await api.portrait();
    } catch (e) {
      // A newer call owns the state now (its result or retry supersedes ours),
      // or we're on our way to the login page.
      if (seq !== portraitSeq || e instanceof AuthExpired) return;
      if (e.status === 404) {
        // She has no portrait yet: her official one, no polling (an `outfit`
        // frame refreshes when hers is drawn).
        portraitHasFrames = false;
        portrait.setFrames({});
        return;
      }
      // Network blip (no status) or 5xx: keep her last good frames — the
      // fallback only if there never were any — and try again later.
      if (!portraitHasFrames) portrait.setFrames({});
      portraitTimer = setTimeout(refreshPortrait, PORTRAIT_POLL_MS * 4);
      return;
    }
    if (seq !== portraitSeq) return; // stale: a newer response already applied
    portraitHasFrames = Boolean(p.frames?.base);
    portrait.setFrames(p.frames || {}, { pending: !portraitHasFrames });
    applyMood();
    const portraitNote = portraitStatusNotice(p.status, { hasFrames: portraitHasFrames });
    if (p.status === "pending" || p.status === "partial") {
      portraitTimer = setTimeout(refreshPortrait, PORTRAIT_POLL_MS);
      // Only while she has no portrait at all; once the base exists the
      // remaining expression frames fill in silently.
      notice(portraitNote, 0);
    } else if (p.status === "unavailable") {
      notice(portraitNote, 6000);
    } else {
      notice("");
    }
  }

  // A frame <img> failed to load — in practice a signed URL that expired
  // (15–30 min) before that expression was first shown. Fetch fresh URLs once,
  // shortly after, however many frames fail together.
  function onPortraitFrameError() {
    if (frameErrorTimer) return;
    frameErrorTimer = setTimeout(() => {
      frameErrorTimer = null;
      const now = Date.now();
      if (now - lastFrameErrorRefresh < FRAME_ERROR_COOLDOWN_MS) return;
      lastFrameErrorRefresh = now;
      refreshPortrait();
    }, FRAME_ERROR_REFRESH_MS);
  }

  function onFrame(frame) {
    const prev = state;
    const { state: next, effects } = reduceFrame(state, frame);
    state = next;
    settle(prev);
    if (frame?.type === "error" && !(typeof frame.message === "string" && frame.message.trim())) {
      notice("Something went wrong — try again.", 4000);
    }
    announceCompleted(frame);
    for (const fx of effects) {
      // One voice channel: her reply is spoken from its `done` text. The
      // server's `voice_audio` clips are dropped in logic.js (see reduceFrame).
      if (fx.kind === "speak") voice?.speak(fx.text);
      else if (fx.kind === "refresh-portrait") refreshPortrait();
      else if (fx.kind === "refresh-day") refreshDay();
    }
  }

  const socket = createSocket({
    url: wsUrl(location, token),
    onFrame,
    onAuthFailed: toLogin,
    onState: (s) => {
      if (s === "down") {
        notice("Link down — reconnecting…", 0);
        // The server never replays a turn lost with the link: stop waiting on it.
        if (state.thinking || state.streaming) {
          const prev = state;
          state = abandonTurn(state);
          settle(prev);
        }
      } else if (s === "open") notice("");
    },
  });

  // ── Wake: the one user gesture iOS needs before any audio can play ──────
  async function startAudio() {
    if (voice && music) return true;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return false;
    const ctx = new AC();
    music = createMusic({ context: ctx });
    voice = createVoice({
      context: ctx,
      tts: api.tts,
      onLevel: (level) => {
        portrait.setLevel(level);
        avatar?.setSpeaking(level);
        stage.classList.toggle("speaking", level > 0.06);
      },
      onSpeaking: (on) => music?.duck(on),
      onError: () => notice("Her voice didn't come through.", 3000),
    });
    voice.setEnabled(prefs.voice);
    try {
      if (prefs.music) await music.start();
      if (ctx.state === "suspended") await ctx.resume();
    } catch {
      return false;
    }
    return ctx.state !== "suspended";
  }

  function showAudioStatus(text) {
    const status = $("audio-status");
    if (!status) return;
    status.hidden = !text;
    status.textContent = text || "";
  }

  async function wake() {
    if (awake) return;
    const plan = beginWake();
    awake = plan.visual;
    $("wake").hidden = plan.hideOverlay;
    coverForWake(false);
    let audioReady = false;
    try {
      audioReady = await startAudio();
    } catch {
      audioReady = false;
    }
    if (!audioReady) {
      notice(plan.audioMissing, 0);
      showAudioStatus(plan.audioMissing);
    } else {
      showAudioStatus("");
    }
    syncButtons();
    applyScene();
    portrait.setActive(prefs.mode === "portrait");
    if (prefs.mode === "model") setMode("model");
    $("input")?.focus();
  }

  function syncButtons() {
    $("btn-music").setAttribute("aria-pressed", String(Boolean(music?.playing)));
    $("btn-voice").setAttribute("aria-pressed", String(Boolean(voice?.enabled)));
    $("btn-voice").textContent = voice?.enabled ? "🔊" : "🔈";
    const m = prefs.mode === "model";
    $("btn-mode").textContent = m ? "2D" : "3D";
    $("btn-mode").setAttribute("aria-label", m ? "Switch to portrait" : "Switch to 3D");
    $("camera-controls").hidden = !m;
    for (const button of $("camera-controls").querySelectorAll("button")) {
      button.disabled = !m || !avatar?.ready;
    }
  }

  const { refreshAppearances, setMode } = createStageAppearance({
    $, api, prefs, savePrefs, notice, portrait, stage, syncButtons, syncInsets,
    runtime: {
      get avatar() { return avatar; },
      set avatar(value) { avatar = value; },
      get state() { return state; },
      get awake() { return awake; },
    },
  });

  // ── Controls ─────────────────────────────────────────────────────────────
  $("wake").addEventListener("click", wake, { once: true });
  $("btn-mode").addEventListener("click", () => setMode(prefs.mode === "model" ? "portrait" : "model"));
  $("btn-zoom-in").addEventListener("click", () => avatar?.zoomBy(0.85));
  $("btn-zoom-out").addEventListener("click", () => avatar?.zoomBy(1 / 0.85));
  $("btn-rotate-left").addEventListener("click", () => avatar?.rotateBy(-Math.PI / 12));
  $("btn-rotate-right").addEventListener("click", () => avatar?.rotateBy(Math.PI / 12));
  $("btn-move-up").addEventListener("click", () => avatar?.moveBy(0.12));
  $("btn-move-down").addEventListener("click", () => avatar?.moveBy(-0.12));
  $("btn-react").addEventListener("click", () => avatar?.react());
  $("btn-animations").addEventListener("click", () => {
    if (!avatar?.ready) return;
    const choices = $("animation-choices");
    choices.replaceChildren();
    const reactions = avatar.getReactions?.() || [];
    for (const category of [...new Set(reactions.map((entry) => entry.category))]) {
      const heading = document.createElement("h3");
      heading.textContent = category;
      choices.appendChild(heading);
      const grid = document.createElement("div");
      grid.className = "animation-grid";
      for (const entry of reactions.filter((item) => item.category === category)) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn";
        button.textContent = entry.label;
        button.dataset.reaction = entry.id;
        button.addEventListener("click", () => {
          $("animation-dialog").close();
          if (!avatar?.react(entry.id)) notice("Give her a moment, then try again.", 2500);
        });
        grid.appendChild(button);
      }
      choices.appendChild(grid);
    }
    if (avatar.getMotionStatus?.() === "unavailable") {
      const message = document.createElement("p");
      message.textContent = "Extra gestures are unavailable right now. These gestures still work.";
      choices.appendChild(message);
    }
    $("animation-dialog").showModal();
  });
  $("btn-reset-view").addEventListener("click", () => avatar?.resetView());
  $("btn-music").addEventListener("click", async () => {
    if (!music) return;
    if (music.playing) music.stop(); else await music.start();
    prefs.music = music.playing;
    savePrefs(prefs);
    syncButtons();
  });
  $("btn-voice").addEventListener("click", async () => {
    if (!voice) {
      let ready = false;
      try { ready = await startAudio(); } catch { ready = false; }
      if (!ready) {
        notice(beginWake().audioMissing, 4000);
        showAudioStatus(beginWake().audioMissing);
        return;
      }
      showAudioStatus("");
    }
    voice.setEnabled(!voice.enabled);
    prefs.voice = voice.enabled;
    savePrefs(prefs);
    syncButtons();
  });
  $("composer").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const input = $("input");
    const content = input.value.trim();
    if (!content) return;
    if (!socket.send({ type: "message", content })) {
      notice("Not connected yet — try again in a moment.", 3000);
      return;
    }
    input.value = "";
    state = { ...state, thinking: true, line: "" };
    renderLine();
  });

  // Parallax: she follows the pointer a few pixels (desktop); calm on touch.
  // Reduced motion clears the tilt and skips new ones.
  const motion = document.querySelector(".portrait-motion");
  if (!reducedNow()) motion.classList.add("parallax");
  const motionQuery = globalThis.matchMedia?.("(prefers-reduced-motion: reduce)");
  motionQuery?.addEventListener?.("change", applyMotionPreference);
  applyMotionPreference();
  $("btn-latest")?.addEventListener("click", () => {
    renderLine({ jumpToLatest: true });
  });
  window.addEventListener("pointermove", (ev) => {
    if (reducedNow()) {
      clearDecorativeMotion(document);
      return;
    }
    if (ev.pointerType !== "mouse") return;
    const dx = (ev.clientX / innerWidth - 0.5) * 8;
    const dy = (ev.clientY / innerHeight - 0.5) * 5;
    motion.style.translate = `${dx.toFixed(1)}px ${dy.toFixed(1)}px`;
  }, { passive: true });

  // ── iOS / Safari lifecycle: suspend in the background, heal on return ──
  document.addEventListener("visibilitychange", () => {
    const hidden = document.hidden;
    music?.setHidden(hidden);
    avatar?.setPaused(hidden || prefs.mode !== "model");
    if (hidden) {
      portrait.setActive(false);
      ambience.stop();
    } else {
      socket.resume();
      ambience.start();
      portrait.setActive(awake && prefs.mode === "portrait");
      refreshDay();
      refreshPortrait(); // her frames may have expired, or finished drawing, while away
    }
  });
  window.addEventListener("pageshow", () => socket.resume());
  window.addEventListener("resize", () => { ambience.resize(); avatar?.resize(); syncInsets(); }, { passive: true });

  coverForWake(true);
  portrait.setFrames({});
  ambience.start();
  // 3D was his last view: start fetching three.js + the model now, in
  // parallel with the socket and the tap-to-begin, so it's ready on wake.
  if (prefs.mode === "model") {
    const modelKey = prefs.appearance || "default";
    import("./avatar3d.js?v=fcbb2d7504d9").then((m) => m.warmAvatar3D?.({ modelKey, loadModel: () => api.model(modelKey) })).catch(() => {});
  }
  socket.start();
  refreshAppearances();
  refreshDay();
  refreshPortrait();
  setInterval(refreshDay, DAY_REFRESH_MS);
  syncButtons();
}

boot();
