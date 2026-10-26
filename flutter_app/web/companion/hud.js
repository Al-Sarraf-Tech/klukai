// The 3D view's HUD, drawn by the stage itself in embed mode (Ani-style, the
// PWA's Tactical HUD look): status chips at the top right (AFFINITY, MOOD,
// HEART, WEARING; only WEARING is a control) and a subtitle card at the
// bottom with her latest line and ▶ VOICE. The PWA sends what to show
// (`klukai:hud`, see embed.js hudUpdate); no PWA widget overlaps the frame
// any more, because Flutter overlays over an iframe flicker on iOS.
//
// Flicker hygiene: the markup is static (companion.html); an update writes
// only the text nodes and attributes that changed, at most once per frame,
// and never touches the canvas. The CSS has no animation loops.

import { readerNearBottom } from "./stage_life.js";

export const PLACEHOLDER = "Awaiting orders, Commander.";

/** ▶ VOICE faces: idle, preparing (synthesis), playing (a tap stops her). */
export const VOICE_FACES = Object.freeze({
  idle: Object.freeze({ icon: "▶", label: "VOICE", aria: "Hear her line" }),
  loading: Object.freeze({ icon: "◌", label: "SYNTH", aria: "Preparing her voice; tap to cancel" }),
  playing: Object.freeze({ icon: "■", label: "STOP", aria: "Stop her voice" }),
});

/**
 * Pure: the voice button's phase after an event. "tap" (his tap: start, or
 * stop when she is busy), "busy" (a reply is being prepared, from any
 * source), "speaking" (a clip started), "done"/"failed" (she finished, was
 * stopped, or could not speak). Gaps between clips stay "playing".
 */
export function nextVoicePhase(phase, event) {
  switch (event) {
    case "tap": return phase === "idle" ? "loading" : "idle";
    case "busy": return phase === "idle" ? "loading" : phase;
    case "speaking": return "playing";
    case "done":
    case "failed": return "idle";
    default: return phase;
  }
}

/**
 * Pure: what the HUD shows for a snapshot (embed.js EMPTY_HUD shape) and the
 * voice phase. While she works on something with no line streaming, her
 * status replaces the line; VOICE is offered for a finished line, and stays
 * (to stop her) while she is preparing or speaking.
 */
export function hudView(snapshot, phase = "idle") {
  const line = String(snapshot.line || "").trim();
  const waiting = snapshot.status !== null && snapshot.status !== undefined && !snapshot.streaming;
  const canSpeak = Boolean(line) && !snapshot.streaming && !waiting;
  return {
    level: `LV${snapshot.level}`,
    mood: snapshot.mood || "—",
    heart: `${snapshot.bpm} bpm`,
    spiking: Boolean(snapshot.spiking),
    wearing: snapshot.wearing || "",
    status: waiting ? String(snapshot.status) : "",
    line: waiting ? "" : line || PLACEHOLDER,
    placeholder: !waiting && !line,
    voice: phase !== "idle" ? phase : canSpeak ? "idle" : "off",
  };
}

const nextFrame = (fn) => (typeof requestAnimationFrame === "function" ? requestAnimationFrame(fn) : setTimeout(fn, 16));

/**
 * Bind the HUD markup. `update(view, { lineId, streaming })` schedules one
 * render per frame; `hold(true)` parks it (the PWA hid the stage) and
 * `hold(false)` renders what came in meanwhile at once.
 */
export function createHud(doc, { onVoice = () => {}, onHerDay = () => {}, schedule = nextFrame } = {}) {
  const $ = (id) => doc.getElementById(id);
  const el = {
    root: $("hud"), level: $("hud-level"), mood: $("hud-mood"), heart: $("hud-heart"), heartTag: $("hud-heart-tag"),
    wearing: $("hud-wearing"), wearingValue: $("hud-wearing-v"), card: $("hud-card"), status: $("hud-status"),
    line: $("hud-line"), voice: $("hud-voice"), voiceIcon: $("hud-voice-icon"), voiceLabel: $("hud-voice-label"),
  };
  el.voice.addEventListener("click", () => onVoice());
  el.wearing.addEventListener("click", () => onHerDay());

  let shown = null;    // the last rendered view
  let lineId;          // the line id last rendered (undefined: none yet)
  let pending = null;  // { view, meta } waiting for a frame
  let queued = false;
  let held = false;

  // Layout can change without new text (rotation, keyboard, or stage reveal).
  // Measure only while visible, without rewriting text or moving his scroll.
  function refreshOverflow() {
    if (held || el.root.hidden || el.line.hidden) return;
    const scrolls = el.line.scrollHeight > el.line.clientHeight + 1;
    if (el.line.classList.contains("scrolls") !== scrolls) el.line.classList.toggle("scrolls", scrolls);
  }

  function renderLine(view, meta) {
    const node = el.line;
    const fresh = meta.lineId !== lineId;
    if (!fresh && shown?.line === view.line) return;
    // Measured before the write: was he reading at the tail?
    const follow = !fresh && meta.streaming && readerNearBottom(node);
    node.textContent = view.line;
    if (fresh) node.scrollTop = 0;
    else if (follow) node.scrollTop = node.scrollHeight;
    lineId = meta.lineId;
    return true;
  }

  function render() {
    queued = false;
    if (!pending || held) return;
    const { view, meta } = pending;
    pending = null;
    const first = !shown;
    const was = shown || {};
    if (was.level !== view.level) el.level.textContent = view.level;
    if (was.mood !== view.mood) el.mood.textContent = view.mood;
    if (was.heart !== view.heart) el.heart.textContent = view.heart;
    if (was.spiking !== view.spiking) el.heartTag.classList.toggle("hot", view.spiking);
    if (was.wearing !== view.wearing) {
      el.wearing.hidden = !view.wearing;
      el.wearingValue.textContent = view.wearing;
      el.wearing.setAttribute("aria-label", view.wearing ? `Her day: wearing ${view.wearing}` : "Her day");
    }
    if (was.status !== view.status) {
      el.status.hidden = !view.status;
      el.status.textContent = view.status ? `◢ ${view.status}` : "";
    }
    if (was.line === undefined || (was.line === "") !== (view.line === "")) el.line.hidden = view.line === "";
    const lineChanged = view.line && renderLine(view, meta);
    if (was.placeholder !== view.placeholder) el.line.classList.toggle("placeholder", view.placeholder);
    if (was.streaming !== meta.streaming) el.card.setAttribute("aria-busy", String(Boolean(meta.streaming)));
    if (was.voice !== view.voice) {
      el.voice.hidden = view.voice === "off";
      const face = VOICE_FACES[view.voice];
      if (face) {
        el.voice.dataset.state = view.voice;
        el.voiceIcon.textContent = face.icon;
        el.voiceLabel.textContent = face.label;
        el.voice.setAttribute("aria-label", face.aria);
      }
    }
    if (!shown) {
      el.root.hidden = false;
      doc.documentElement.dataset.hud = "1";
    }
    shown = { ...view, streaming: meta.streaming };
    if (first || lineChanged) refreshOverflow();
  }

  return {
    update(view, meta = {}) {
      pending = { view, meta: { lineId: meta.lineId ?? null, streaming: Boolean(meta.streaming) } };
      if (queued || held) return;
      queued = true;
      schedule(render);
    },
    hold(on) {
      held = Boolean(on);
      if (!held) {
        if (pending) render();
        refreshOverflow();
      }
    },
    resize: refreshOverflow,
    flush: render,
  };
}
