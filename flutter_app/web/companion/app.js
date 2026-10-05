// Companion stage — wiring. Live portrait (default) or 3D window, her voice,
// a generative score, and a chat dock, all on the same session as the PWA
// (messages sent here appear there too: the server fans out to every device).

import { readToken, clearToken, createApi, wsUrl, createSocket } from "./net.js";
import { moodToExpression, sceneFor, reduceFrame } from "./logic.js";
import { createPortrait } from "./portrait.js";
import { createAmbience, ambienceKind } from "./ambience.js";
import { createMusic } from "./music.js";
import { createVoice } from "./voice.js";

const $ = (id) => document.getElementById(id);
const PREFS_KEY = "klukai_companion_prefs";
const DAY_REFRESH_MS = 5 * 60 * 1000;
const PORTRAIT_POLL_MS = 15000;
const TINTS = {
  base: "rgba(79, 195, 247, 0.10)", smile: "rgba(232, 140, 165, 0.14)",
  blush: "rgba(232, 140, 165, 0.20)", annoyed: "rgba(232, 146, 62, 0.14)",
};

function loadPrefs() {
  try { return { mode: "portrait", music: true, voice: true, ...JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") }; }
  catch { return { mode: "portrait", music: true, voice: true }; }
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
  const portrait = createPortrait($("portrait"));
  const ambience = createAmbience($("ambience"));
  let state = { line: "", mood: "composed", thinking: false, streaming: false };
  let day = null;
  let music = null;
  let voice = null;
  let avatar = null;
  let portraitTimer = null;
  let noticeTimer = null;
  let awake = false;

  const notice = (text, ms = 4000) => {
    const n = $("notice");
    n.textContent = text;
    n.hidden = !text;
    clearTimeout(noticeTimer);
    if (text && ms) noticeTimer = setTimeout(() => { n.hidden = true; }, ms);
  };

  function renderLine() {
    const sub = $("subtitle");
    sub.textContent = state.line;
    sub.classList.toggle("thinking", state.thinking);
    sub.scrollTop = sub.scrollHeight;
  }

  function applyMood() {
    const expr = moodToExpression(state.mood);
    portrait.setExpression(expr);
    stage.style.setProperty("--tint", TINTS[expr] || TINTS.base);
    avatar?.setMood(state.mood);
    applyScene();
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
    clearTimeout(portraitTimer);
    try {
      const p = await api.portrait();
      portrait.setFrames(p.frames || {}, { pending: !p.frames?.base });
      applyMood();
      if (p.status === "pending" || p.status === "partial") {
        portraitTimer = setTimeout(refreshPortrait, PORTRAIT_POLL_MS);
        // Only while she has no portrait at all; once the base exists the
        // remaining expression frames fill in silently.
        notice(p.frames?.base ? "" : "She's getting ready — her portrait is being drawn.", 0);
      } else if (p.status === "unavailable") {
        notice("She's off-camera for now — the GPU is busy.", 6000);
      } else {
        notice("");
      }
    } catch (e) {
      portrait.setFrames({}); // her official portrait until frames exist
      if (e.status && e.status !== 404) portraitTimer = setTimeout(refreshPortrait, PORTRAIT_POLL_MS * 4);
    }
  }

  function onFrame(frame) {
    const prevMood = state.mood;
    const { state: next, effects } = reduceFrame(state, frame);
    state = next;
    renderLine();
    if (state.mood !== prevMood) applyMood();
    if (state.thinking) avatar?.setState("thinking");
    for (const fx of effects) {
      if (fx.kind === "speak") voice?.speak(fx.text);
      else if (fx.kind === "play-audio") voice?.play(fx.audio);
      else if (fx.kind === "refresh-portrait") refreshPortrait();
      else if (fx.kind === "refresh-day") refreshDay();
    }
  }

  const socket = createSocket({
    url: wsUrl(location, token),
    onFrame,
    onAuthFailed: toLogin,
    onState: (s) => {
      if (s === "down") notice("Link down — reconnecting…", 0);
      else if (s === "open") notice("");
    },
  });

  // ── Wake: the one user gesture iOS needs before any audio can play ──────
  async function wake() {
    if (awake) return;
    awake = true;
    $("wake").hidden = true;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (AC) {
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
      if (prefs.music) await music.start();
      if (ctx.state === "suspended") await ctx.resume();
    }
    syncButtons();
    applyScene();
    portrait.setActive(prefs.mode === "portrait");
    if (prefs.mode === "model") setMode("model");
  }

  function syncButtons() {
    $("btn-music").setAttribute("aria-pressed", String(Boolean(music?.playing)));
    $("btn-voice").setAttribute("aria-pressed", String(Boolean(voice?.enabled)));
    $("btn-voice").textContent = voice?.enabled ? "🔊" : "🔈";
    const m = prefs.mode === "model";
    $("btn-mode").textContent = m ? "2D" : "3D";
    $("btn-mode").setAttribute("aria-label", m ? "Switch to portrait" : "Switch to 3D");
  }

  // ── Portrait ⇄ 3D window ─────────────────────────────────────────────────
  async function setMode(mode) {
    prefs.mode = mode;
    savePrefs(prefs);
    stage.dataset.mode = mode;
    const model = $("model");
    if (mode === "model") {
      portrait.setActive(false);
      model.hidden = false;
      if (!avatar) {
        notice("Loading her 3D model…", 0);
        try {
          const { createAvatar3D } = await import("./avatar3d.js");
          avatar = await createAvatar3D(model, {
            loadModel: api.model,
            onReady: () => notice(""),
            onError: (e) => {
              notice(e && e.status === 404 ? "Her 3D model isn't installed on the server yet." : "3D isn't available on this device.", 6000);
              avatar = null;
              setMode("portrait");
            },
          });
          avatar?.setMood(state.mood);
        } catch {
          notice("3D isn't available on this device.", 6000);
          avatar = null;
          setMode("portrait");
          return;
        }
      }
    } else {
      model.hidden = true;
      portrait.setActive(awake);
    }
    syncButtons();
  }

  // ── Controls ─────────────────────────────────────────────────────────────
  $("wake").addEventListener("click", wake, { once: true });
  $("btn-mode").addEventListener("click", () => setMode(prefs.mode === "model" ? "portrait" : "model"));
  $("btn-music").addEventListener("click", async () => {
    if (!music) return;
    if (music.playing) music.stop(); else await music.start();
    prefs.music = music.playing;
    savePrefs(prefs);
    syncButtons();
  });
  $("btn-voice").addEventListener("click", () => {
    if (!voice) return;
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
  const motion = document.querySelector(".portrait-motion");
  motion.classList.add("parallax");
  window.addEventListener("pointermove", (ev) => {
    if (ev.pointerType !== "mouse") return;
    const dx = (ev.clientX / innerWidth - 0.5) * 8;
    const dy = (ev.clientY / innerHeight - 0.5) * 5;
    motion.style.translate = `${dx.toFixed(1)}px ${dy.toFixed(1)}px`;
  }, { passive: true });

  // ── iOS / Safari lifecycle: suspend in the background, heal on return ──
  document.addEventListener("visibilitychange", () => {
    const hidden = document.hidden;
    music?.setHidden(hidden);
    if (hidden) {
      portrait.setActive(false);
      ambience.stop();
    } else {
      socket.resume();
      ambience.start();
      portrait.setActive(awake && prefs.mode === "portrait");
      refreshDay();
    }
  });
  window.addEventListener("pageshow", () => socket.resume());
  window.addEventListener("resize", () => { ambience.resize(); avatar?.resize(); }, { passive: true });

  portrait.setFrames({});
  ambience.start();
  socket.start();
  refreshDay();
  refreshPortrait();
  setInterval(refreshDay, DAY_REFRESH_MS);
  syncButtons();
}

boot();
