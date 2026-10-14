// Companion stage — wiring. Live portrait (default) or 3D window, her voice,
// a generative score, and a chat dock, all on the same session as the PWA
// (messages sent here appear there too: the server fans out to every device).

import { readToken, clearToken, createApi, AuthExpired, wsUrl, createSocket } from "./net.js?v=ed5305f8";
import { moodToExpression, sceneFor, reduceFrame, abandonTurn, avatarCue } from "./logic.js";
import { createPortrait } from "./portrait.js";
import { createAmbience, ambienceKind } from "./ambience.js";
import { createMusic } from "./music.js";
import { createVoice } from "./voice.js?v=12e6e4bd";
import { createAppearanceSwitcher } from "./appearance.js";

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
    if (p.status === "pending" || p.status === "partial") {
      portraitTimer = setTimeout(refreshPortrait, PORTRAIT_POLL_MS);
      // Only while she has no portrait at all; once the base exists the
      // remaining expression frames fill in silently.
      notice(portraitHasFrames ? "" : "She's getting ready — her portrait is being drawn.", 0);
    } else if (p.status === "unavailable") {
      notice("She's off-camera for now — the GPU is busy.", 6000);
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
  let models = [];
  let requestedAppearance = prefs.appearance || "default";
  const appearances = createAppearanceSwitcher({
    async build(id, signal) {
      const { createAvatar3D } = await import("./avatar3d.js");
      const host = document.createElement("div");
      host.className = "model-view";
      host.style.visibility = "hidden";
      $("model").appendChild(host);
      signal.addEventListener("abort", () => host.remove(), { once: true });
      let error;
      try {
        const av = await createAvatar3D(host, {
          modelKey: id, signal, loadModel: () => api.model(id, { signal }),
          onError: (e) => { error = e; },
        });
        const dispose = av.dispose.bind(av);
        av.dispose = () => { dispose(); host.remove(); };
        if (!av.ready) { av.dispose(); throw error || new Error("3D unavailable"); }
        av.reveal = () => { host.style.visibility = ""; };
        return av;
      } catch (e) { host.remove(); throw e; }
    },
    commit(av, id) {
      av.reveal();
      avatar = av;
      prefs.appearance = id;
      savePrefs(prefs);
      $("appearance-select").value = id;
      renderAppearanceCredit(id);
      avatar.setTint?.(moodToExpression(state.mood));
      avatar.setMood(state.mood);
      if (state.thinking) avatar.setState("thinking");
      avatar.setPaused(prefs.mode !== "model" || document.hidden);
      syncInsets();
      notice("");
    },
  });

  function renderAppearanceCredit(id) {
    const entry = models.find((m) => m.id === id);
    $("appearance-credit").textContent = entry ? `${entry.creator} · ${entry.license}` : "";
    $("appearance-description").textContent = entry?.description || "";
    const link = $("appearance-source");
    const url = entry?.source || "";
    link.hidden = !url.startsWith("https://");
    if (!link.hidden) link.href = url;
    else link.removeAttribute("href");
  }

  async function refreshAppearances() {
    try {
      const result = await api.models();
      models = result.models;
      const select = $("appearance-select");
      select.replaceChildren(...models.map((m) => {
        const option = document.createElement("option");
        option.value = m.id;
        option.textContent = m.name + (m.available ? "" : " · Not installed");
        option.disabled = !m.available;
        return option;
      }));
      select.disabled = false;
      if (!models.some((m) => m.id === prefs.appearance && m.available)) {
        const changed = prefs.appearance !== "default";
        prefs.appearance = "default";
        requestedAppearance = "default";
        savePrefs(prefs);
        if (changed && awake && prefs.mode === "model") await setMode("model");
      }
      select.value = prefs.appearance;
      renderAppearanceCredit(prefs.appearance);
    } catch { /* keep the pinned default usable if the catalog is temporarily unavailable */ }
  }

  async function setMode(mode, appearance = requestedAppearance) {
    requestedAppearance = appearance;
    prefs.mode = mode;
    savePrefs(prefs);
    stage.dataset.mode = mode;
    const model = $("model");
    if (mode === "model") {
      portrait.setActive(false);
      model.hidden = false;
      if (appearances.key !== appearance) notice("Loading her 3D model…", 0);
      try {
        const applied = await appearances.select(appearance);
        if (!applied) return;
        notice("");
      } catch (e) {
        requestedAppearance = appearances.key || prefs.appearance;
        $("appearance-select").value = appearances.key || prefs.appearance;
        notice(e?.status === 404 ? "That appearance isn't installed yet." : "Couldn't load that appearance. Try again.", 6000);
        if (!avatar) {
          prefs.mode = "portrait";
          savePrefs(prefs);
          stage.dataset.mode = "portrait";
          model.hidden = true;
          portrait.setActive(awake);
        }
      }
      syncInsets();
    } else {
      model.hidden = true;
      portrait.setActive(awake);
    }
    avatar?.setPaused(prefs.mode !== "model" || document.hidden);
    syncButtons();
  }

  $("btn-appearance").addEventListener("click", () => {
    $("appearance-dialog").showModal();
    refreshAppearances();
  });
  $("appearance-select").addEventListener("change", (event) => {
    const id = event.target.value;
    if (!models.some((m) => m.id === id && m.available)) return;
    $("appearance-dialog").close();
    setMode("model", id);
  });

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

  portrait.setFrames({});
  ambience.start();
  // 3D was his last view: start fetching three.js + the model now, in
  // parallel with the socket and the tap-to-begin, so it's ready on wake.
  if (prefs.mode === "model") {
    const modelKey = prefs.appearance || "default";
    import("./avatar3d.js").then((m) => m.warmAvatar3D?.({ modelKey, loadModel: () => api.model(modelKey) })).catch(() => {});
  }
  socket.start();
  refreshAppearances();
  refreshDay();
  refreshPortrait();
  setInterval(refreshDay, DAY_REFRESH_MS);
  syncButtons();
}

boot();
