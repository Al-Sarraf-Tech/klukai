import { DEG, clamp, smoothstep } from './common.js';

export function speechMouthDelta([x, y, z], seamY = 1.3365) {
  if (Math.abs(x) >= 0.03 || y < 1.312 || y > 1.355 || z < 0.025 || z > 0.065) return [0, 0, 0];
  const seam = seamY + 0.0015 * Math.min(1, (x / 0.011) ** 2);
  const width = 1 - smoothstep(0.008, 0.03, Math.abs(x));
  const edge = y < seam ? smoothstep(1.312, 1.331, y) : 1 - smoothstep(1.341, 1.355, y);
  const lower = y < seam;
  return [0, (lower ? -0.007 : 0.002) * width * edge, 0];
}

/** Noise floor suppresses resting hiss; low conversational levels still read. */
export function speechMouthLevel(level) {
  return Math.pow(clamp((Number(level) - 0.025) / 0.65 || 0, 0, 1), 0.7);
}

export const LOOK_WEIGHT = Object.freeze({
  idle: 1, talking: 0.8, happy: 0.7, thinking: 0.35, bashful: 0.4, sad: 0.3, greet: 0.6, sleep: 0, excited: 0.6, flustered: 0.3,
});

/**
 * Pure: clamp a look correction (radians; yaw + = toward +x, pitch + = up) and
 * split it over the bones. With a skinned Head joint: neck 40 % / head 60 %,
 * yaw ±35°, pitch −15°…+20°. Without one (the head mesh rides on the Neck):
 * everything on the neck, yaw ±25°, pitch −15°…+15°.
 */
export function lookSplit({ yaw = 0, pitch = 0, headJoint = false } = {}) {
  const yawMax = (headJoint ? 35 : 25) * DEG;
  const up = (headJoint ? 20 : 15) * DEG;
  const down = 15 * DEG;
  const y = clamp(Number(yaw) || 0, -yawMax, yawMax);
  const p = clamp(Number(pitch) || 0, -down, up);
  if (headJoint) return { neck: { yaw: y * 0.4, pitch: p * 0.4 }, head: { yaw: y * 0.6, pitch: p * 0.6 } };
  return { neck: { yaw: y, pitch: p }, head: { yaw: 0, pitch: 0 } };
}

/** Eye guard (degrees): camera elevation above the face's own plane where the
 *  lift starts, and the soft ceiling it eases toward (irises stay under the brim);
 *  both come down by `side` when he sees her face from the side (≥ 70° azimuth).
 *  Calibrated with tools/avatar/debug/eyes.py: ≤ 0° both irises clear the brim,
 *  3–5° the brim covers their upper half, ≥ 8° they are gone. */
export const EYE_GUARD = Object.freeze({ start: -3, max: 1, side: 6 });

/** Pure: soft ceiling. x ≤ start passes through; above it the value eases toward
 *  `max` (tanh), with a continuous slope at `start`. */
export function softLimit(x, start, max) {
  if (!(x > start)) return x;
  const span = max - start;
  return span > 0 ? start + span * Math.tanh((x - start) / span) : start;
}

/** Saturated rim colours per portrait expression (app.js `moodToExpression`). */
