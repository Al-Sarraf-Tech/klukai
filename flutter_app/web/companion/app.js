// Companion stage — wiring. Live portrait (default) or 3D window, her voice,
// a generative score, and a chat dock, all on the same session as the PWA
// (messages sent here appear there too: the server fans out to every device).

import { readToken, clearToken, createApi, AuthExpired, wsUrl, createSocket } from "./net.js?v=ed76f4b9";
import { moodToExpression, sceneFor, reduceFrame, abandonTurn, avatarCue } from "./logic.js";
import { createPortrait } from "./portrait.js";
import { createAmbience, ambienceKind } from "./ambience.js";
import { createMusic } from "./music.js";
import { createVoice } from "./voice.js?v=20ea8f11";
import { createStageAppearance } from "./stage_appearance.js?v=de4fa2ed";
import {
  applySubtitle, completedAnnouncement, clearDecorativeMotion, motionPreference,
  beginWake, portraitStatusNotice,
} from "./stage_life.js";
import { createHud, hudView, nextVoicePhase } from "./hud.js?v=1ca756c7";
import {
  embedConfig, speakRequest, visibilityRequest, hudUpdate, HERDAY, stageActivity, createSpeakGate, resumeWithin, postToParent,
} from "./embed.js?v=8b18934d";

const $ = (id) => document.getElementById(id);
const PREFS_KEY = "klukai_companion_prefs";
const DAY_REFRESH_MS = 5 * 60 * 1000;
const AUDIO_RESUME_MS = 3000; // resume() can wait forever without a gesture
// Inside the PWA's 3D view (companion.html?embed=1): framed waist-up, own camera prefs.
const EMBED = embedConfig(globalThis.location?.search || "");
if (EMBED.embed) document.documentElement.dataset.embed = "1";
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

// Back to the login page; embedded, the PWA owns that (it revokes and redirects).
function leave() {
  if (EMBED.embed) postToParent(window, { type: "klukai:auth-expired" });
  else location.replace("/");
}

function boot() {
  // Her latest finished line, played only from the voice button.
  let lastLine = "";
  const token = readToken();
  if (!token) { leave(); return; }
  // His session ended: drop the token and her downloaded model, then leave.
  let forgetModel = () => {}; // set once the stage appearance exists
  const toLogin = () => { clearToken(); forgetModel(); leave(); };

  const stage = $("stage");
  const prefs = loadPrefs();
  // Embedded, the stage always shows 3D, without overwriting the mode he
  // chose on the full stage.
  const storedMode = prefs.mode;
  if (EMBED.embed) prefs.mode = "model";
  const persistPrefs = EMBED.embed ? (p) => savePrefs({ ...p, mode: storedMode }) : savePrefs;
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
  let audioCtx = null;
  let reportedReady = null;
  // Embedded: the PWA parks this page behind its CHAT face instead of
  // unloading it, so 3D is instant when he flips back (klukai:visibility).
  let embedHidden = EMBED.hidden;
  // Embedded: the 3D view's HUD (status chips, her latest line, ▶ VOICE),
  // drawn here from the PWA's klukai:hud snapshots (hud.js). Its VOICE is a
  // tap in this document, the gesture iOS needs to unlock audio here.
  let hudState = null;      // the latest snapshot (null until the PWA sends one)
  let voicePhase = "idle";  // idle | loading | playing (the HUD's VOICE button)
  let voiceTicket = 0;      // a newer tap (or a stop) supersedes a pending one
  const hud = EMBED.embed
    ? createHud(document, { onVoice: onHudVoice, onHerDay: () => postToParent(window, { type: HERDAY }) })
    : null;
  if (hud) hud.hold(embedHidden);

  const notice = (text, ms = 4000) => {
    // Embedded, the notice element is hidden: the PWA's readout shows it.
    if (EMBED.embed) postToParent(window, { type: "klukai:stage-notice", text: text || "" });
    const n = $("notice");
    n.textContent = text;
    n.hidden = !text;
    clearTimeout(noticeTimer);
    if (text && ms) {
      noticeTimer = setTimeout(() => {
        n.hidden = true;
        // The PWA keeps the stage across flips: don't leave it a stale notice.
        if (EMBED.embed) postToParent(window, { type: "klukai:stage-notice", text: "" });
      }, ms);
    }
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
    if (EMBED.embed) { avatar?.setInsets?.(EMBED.insets); return; } // the PWA's HUD, not ours
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
      // Owner 2026-10-19: her voice plays ONLY when he asks. A finished reply
      // is remembered, never spoken; the voice button plays it on a tap.
      if (fx.kind === "speak") { lastLine = fx.text; syncButtons(); }
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
  // The audio context, her voice and the score, created on first use.
  function audioContextNow() {
    if (audioCtx) return audioCtx;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    const ctx = new AC();
    audioCtx = ctx;
    music = createMusic({ context: ctx });
    voice = createVoice({
      context: ctx,
      tts: api.tts,
      onLevel: (level) => {
        portrait.setLevel(level);
        avatar?.setSpeaking(level);
        stage.classList.toggle("speaking", level > 0.06);
      },
      onSpeaking: (on) => {
        music?.duck(on);
        if (on) setVoicePhase("speaking");
        if (EMBED.embed) postToParent(window, { type: "klukai:speaking", on });
      },
      onBusy: (busy) => setVoicePhase(busy ? "busy" : "done"),
      onError: () => {
        if (EMBED.embed) postToParent(window, { type: "klukai:voice-error" });
        notice("Her voice didn't come through.", 3000);
      },
    });
    voice.setEnabled(true);  // speaks only from the voice button (tap-to-play)
    return ctx;
  }

  async function startAudio() {
    const fresh = !audioCtx;
    const ctx = audioContextNow();
    if (!ctx) return false;
    if (fresh) {
      try {
        if (prefs.music && !EMBED.embed) await music.start(); // the PWA has its own ambience
      } catch {
        return false;
      }
    }
    return resumeWithin(ctx, AUDIO_RESUME_MS);
  }

  // Inside his tap, before any await: create/resume the context and start a
  // silent sample, so WebKit (iOS) unlocks audio for this document.
  function unlockAudio() {
    const ctx = audioContextNow();
    if (!ctx) return;
    try { Promise.resolve(ctx.resume?.()).catch(() => {}); } catch { /* resumeWithin reports it */ }
    try {
      const src = ctx.createBufferSource();
      src.buffer = ctx.createBuffer(1, 1, 22050);
      src.connect(ctx.destination);
      src.start(0);
    } catch { /* not fatal: resumeWithin reports the state */ }
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
    $("btn-voice").disabled = !lastLine;
    $("btn-voice").setAttribute("aria-label", lastLine ? "Hear her last line" : "Her voice (nothing to play yet)");
    const m = prefs.mode === "model";
    $("btn-mode").textContent = m ? "2D" : "3D";
    $("btn-mode").setAttribute("aria-label", m ? "Switch to portrait" : "Switch to 3D");
    $("camera-controls").hidden = !m;
    for (const button of $("camera-controls").querySelectorAll("button")) {
      button.disabled = !m || !avatar?.ready;
    }
    if (EMBED.embed) reportReady();
  }

  // Embedded: tell the PWA when she is on screen (3D, or the portrait it
  // falls back to when 3D can't load), so its readout says TRACKING.
  function reportReady() {
    const ready = awake && (prefs.mode === "model" ? Boolean(avatar?.ready) : true);
    if (ready === reportedReady) return;
    reportedReady = ready;
    postToParent(window, { type: "klukai:stage-ready", ready });
  }

  // Embedded: the PWA's `▶ VOICE` (a tap there) asks for a line. Played once
  // per request, lip-synced; replaces whatever she was saying. If audio
  // can't start, the PWA is told and plays the line itself.
  function listenForSpeech() {
    const gate = createSpeakGate();
    window.addEventListener("message", async (event) => {
      const request = speakRequest(event, { origin: location.origin, parent: window.parent });
      if (!gate.admit(request)) return;
      const reply = (status) => postToParent(window, { type: "klukai:speak-status", id: request.id, status });
      if (embedHidden) { reply("hidden"); return; }
      lastLine = request.text;
      syncButtons();
      let ready = false;
      try { ready = await startAudio(); } catch { ready = false; }
      // He flipped back to CHAT while the audio was starting: stay quiet.
      if (embedHidden) { music?.setHidden(true); reply("hidden"); return; }
      if (!ready || !voice) { reply("unavailable"); return; }
      voice.stop();
      reply(voice.speak(request.text) ? "playing" : "empty");
    });
  }

  // ── Embedded HUD ─────────────────────────────────────────────────────────
  function renderHud() {
    if (!hud || !hudState) return;
    hud.update(hudView(hudState, voicePhase), { lineId: hudState.lineId, streaming: hudState.streaming });
  }

  function setVoicePhase(event) {
    const next = nextVoicePhase(voicePhase, event);
    if (next === voicePhase) return;
    voicePhase = next;
    renderHud();
  }

  // The PWA's snapshot of what the HUD shows (same strict origin + source
  // check as a speak request). A snapshot never makes a sound.
  function listenForHud() {
    window.addEventListener("message", (event) => {
      const next = hudUpdate(event, { origin: location.origin, parent: window.parent }, hudState || undefined);
      if (!next) return;
      hudState = next;
      renderHud();
    });
  }

  // ▶ VOICE on the stage's HUD: his tap, in this document. Speaks her line
  // once, lip-synced; a tap while she is preparing or speaking stops her.
  async function onHudVoice() {
    if (voicePhase !== "idle") {
      voiceTicket++;
      voice?.stop();
      setVoicePhase("tap");
      return;
    }
    const line = hudState && !hudState.streaming ? hudState.line.trim() : "";
    if (!line || embedHidden) return;
    const ticket = ++voiceTicket;
    unlockAudio();
    setVoicePhase("tap");
    let ready = false;
    try { ready = await startAudio(); } catch { ready = false; }
    if (ticket !== voiceTicket || embedHidden) return; // stopped, or he flipped back to CHAT
    if (!ready || !voice) {
      setVoicePhase("failed");
      postToParent(window, { type: "klukai:voice-error" });
      notice("Her voice didn't come through.", 3000);
      return;
    }
    lastLine = line;
    voice.stop();
    if (!voice.speak(line)) setVoicePhase("failed");
  }

  // What runs follows one rule (embed.js stageActivity): on screen means the
  // page is visible and, embedded, the PWA shows its 3D face. Off screen the
  // render loop, ambience and portrait stop and the audio context suspends.
  function applyActivity() {
    const run = stageActivity({ documentHidden: document.hidden, embedHidden, mode: prefs.mode, awake });
    music?.setHidden(!run.audio);
    avatar?.setPaused(!run.render);
    portrait.setActive(run.portrait);
    if (run.ambience) ambience.start(); else ambience.stop();
  }

  // Embedded: the PWA's CHAT ⇄ 3D flip. Hidden, she stops talking (as if the
  // page were gone) and nothing draws; shown, she picks up where she was —
  // silently: only a tap on ▶ VOICE ever makes her speak.
  function listenForVisibility() {
    window.addEventListener("message", (event) => {
      const request = visibilityRequest(event, { origin: location.origin, parent: window.parent });
      if (!request || request.visible === !embedHidden) return;
      embedHidden = !request.visible;
      if (embedHidden) {
        voice?.stop();
        voiceTicket++;
        setVoicePhase("done");
      }
      hud?.hold(embedHidden);
      applyActivity();
      if (!embedHidden) socket.resume();
    });
  }

  const { refreshAppearances, setMode, warmModel, forgetModels: forgetStageModels } = createStageAppearance({
    $, api, prefs, savePrefs: persistPrefs, notice, portrait, stage, syncButtons, syncInsets,
    framing: EMBED.embed ? "waist" : "full",
    viewKey: EMBED.embed ? "embedView" : "view",
    doubleTapReset: EMBED.embed,
    runtime: {
      get avatar() { return avatar; },
      set avatar(value) { avatar = value; },
      get state() { return state; },
      get awake() { return awake; },
      get hidden() { return document.hidden || embedHidden; },
    },
  });
  forgetModel = forgetStageModels;

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
    if (!lastLine) { notice("Nothing for her to say yet.", 2000); return; }
    voice.setEnabled(true);
    voice.speak(lastLine);
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
    applyActivity();
    if (!document.hidden) {
      socket.resume();
      refreshDay();
      refreshPortrait(); // her frames may have expired, or finished drawing, while away
    }
  });
  window.addEventListener("pageshow", () => socket.resume());
  window.addEventListener("resize", () => { ambience.resize(); avatar?.resize(); syncInsets(); }, { passive: true });

  if (EMBED.embed) {
    // The PWA already had his tap: no wake overlay. Visual wake only; audio
    // starts with the first speak request (itself a tap in the PWA).
    awake = true;
    $("wake").hidden = true;
    coverForWake(false);
    // iOS Safari pinch-zooms the page on a two-finger gesture (it ignores
    // user-scalable): here the pinch belongs to her camera.
    for (const type of ["gesturestart", "gesturechange"]) {
      document.addEventListener(type, (event) => event.preventDefault(), { passive: false });
    }
    listenForVisibility();
    listenForSpeech();
    listenForHud();
    setMode("model");
  } else coverForWake(true);
  portrait.setFrames({});
  if (!embedHidden) ambience.start();
  // 3D was his last view: start fetching three.js + the model now, in
  // parallel with the socket and the tap-to-begin, so it's ready on wake.
  // (Embedded, setMode above is already loading it: this joins that download.)
  warmModel();
  socket.start();
  refreshAppearances();
  refreshDay();
  refreshPortrait();
  setInterval(refreshDay, DAY_REFRESH_MS);
  syncButtons();
}

boot();
