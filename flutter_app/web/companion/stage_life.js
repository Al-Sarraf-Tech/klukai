/** Subtitle follow, wake, announcements, and reduced-motion resets. */

const NEAR_BOTTOM_PX = 48;

export function readerNearBottom(el, slack = NEAR_BOTTOM_PX) {
  if (!el) return true;
  const height = Number(el.clientHeight) || 0;
  const scroll = Number(el.scrollHeight) || 0;
  const top = Number(el.scrollTop) || 0;
  if (scroll <= height + slack) return true;
  return scroll - height - top <= slack;
}

/**
 * Write subtitle text. Scroll only when the reader was already near the
 * bottom, or when they ask for Latest. A reader who scrolled up stays put.
 */
export function applySubtitle(el, text, { jumpToLatest = false, slack = NEAR_BOTTOM_PX } = {}) {
  const followed = Boolean(jumpToLatest || readerNearBottom(el, slack));
  const previous = Number(el.scrollTop) || 0;
  el.textContent = text ?? "";
  el.scrollTop = followed ? Number(el.scrollHeight) || 0 : previous;
  return { followed };
}

/** One polite announcement per completed reply. Tokens return null. */
export function completedAnnouncement(frame) {
  if (!frame) return null;
  if (frame.type === "token" || frame.type === "thinking") return null;
  if (frame.type === "done") {
    const text = typeof frame.text === "string" ? frame.text.trim() : "";
    return text || null;
  }
  if (frame.type === "proactive") {
    const text = typeof frame.message === "string" ? frame.message.trim() : "";
    return text || null;
  }
  return null;
}

export function motionPreference(media) {
  return Boolean(media && media.matches === true);
}

/** Drop decorative translate/transform so a preference change cannot leave a tilt. */
export function clearDecorativeMotion(root) {
  if (!root || typeof root.querySelectorAll !== "function") return false;
  for (const el of root.querySelectorAll(".portrait-motion, .subtitle")) {
    if (el.style) {
      el.style.translate = "0px 0px";
      el.style.transform = "none";
      el.style.rotate = "0deg";
    }
    el.classList?.remove("parallax");
  }
  return true;
}

/** Visual and text mode always proceed. Audio failure is a status, not a trap. */
export function beginWake() {
  return {
    visual: true,
    hideOverlay: true,
    audioMissing: "Audio didn't start. Text still works.",
  };
}

export function portraitStatusNotice(status, { hasFrames = false } = {}) {
  if (status === "pending" || status === "partial") {
    return hasFrames ? "" : "She's getting ready — her portrait is being drawn.";
  }
  if (status === "unavailable") return "She's off-camera for now.";
  return "";
}
