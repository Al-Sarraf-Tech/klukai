// The PWA's embedded 3D view (companion.html?embed=1): Ani-style waist-up
// framing, a freer camera, and the double-tap reset. Pure parts only.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PerspectiveCamera, Spherical, Vector3 } from 'three';
import {
  cameraFraming, cameraLimits, CAMERA_ZOOM_RANGE, createCameraView, normalizeFraming, zoomAim,
  blendView, createViewTween, createDoubleTap, contactShadowWanted, followGoal, WAIST_LIMITS, WAIST_AIM,
} from '../../../flutter_app/web/companion/avatar/camera.js';

const HEAD_Y = 1.36;
const close = (actual, expected, eps = 1e-9) => assert.ok(Math.abs(actual - expected) < eps, `${actual} ≠ ${expected}`);

/** A camera as the runtime places it: framing + user view (+ the zoom aim). */
function place({ width = 390, height = 620, framing = 'waist', view = {}, insets = null } = {}) {
  const f = cameraFraming({ framing, aspect: width / height, headY: HEAD_Y, insets });
  const orbit = createCameraView(view, cameraLimits(framing));
  const base = new Spherical().setFromVector3(new Vector3(0, f.camY - f.targetY, f.dist));
  const placed = orbit.place(base);
  const target = new Vector3(0, f.targetY + orbit.state.panY + zoomAim({ framing, zoom: orbit.state.zoom, targetY: f.targetY, headY: HEAD_Y }), 0);
  const camera = new PerspectiveCamera(f.fov, width / height, 0.05, 60);
  camera.position.setFromSpherical(new Spherical(placed.radius, placed.phi, placed.theta)).add(target);
  camera.lookAt(target);
  camera.updateMatrixWorld();
  camera.updateProjectionMatrix();
  const screenY = (y) => (1 - new Vector3(0, y, 0).project(camera).y) * height / 2;
  return { f, camera, screenY, target, placed, orbit };
}

test('framing names: full and waist are kept, anything else is the upper crop', () => {
  assert.equal(normalizeFraming('full'), 'full');
  assert.equal(normalizeFraming('waist'), 'waist');
  for (const other of ['upper', undefined, null, 'WAIST', 'close', 3]) assert.equal(normalizeFraming(other), 'upper');
});

test('waist framing fills a phone iframe waist-up: big head, hips at the bottom edge, legs out of frame', () => {
  for (const [width, height] of [[390, 620], [360, 560], [390, 700], [430, 760]]) {
    const { screenY } = place({ width, height });
    const crown = screenY(HEAD_Y + 0.18), chin = screenY(HEAD_Y - 0.12);
    assert.ok(crown > 0 && crown < height * 0.12, `${width}×${height}: cap crown at ${crown.toFixed(0)} px sits just under the top`);
    assert.ok((chin - crown) / height > 0.28, `${width}×${height}: her head is ${((chin - crown) / height * 100).toFixed(0)}% of the frame`);
    assert.ok(screenY(0.95) < height, `${width}×${height}: her waist is in frame`);
    assert.ok(screenY(0.45) > height, `${width}×${height}: knees are below the frame (not full body)`);
  }
});

test('waist framing is the upper crop with the camera at chest height, level or looking slightly up', () => {
  for (const aspect of [390 / 620, 360 / 560, 1.6]) {
    const waist = cameraFraming({ framing: 'waist', aspect, headY: HEAD_Y });
    const upper = cameraFraming({ framing: 'upper', aspect, headY: HEAD_Y });
    close(waist.targetY, upper.targetY);
    close(waist.dist, upper.dist);
    assert.equal(waist.fov, upper.fov);
    assert.ok(upper.camY > upper.targetY, 'the standalone upper crop still looks slightly down');
    assert.ok(waist.camY <= waist.targetY && waist.targetY - waist.camY < 0.08, `level/slightly up: cam ${waist.camY} vs target ${waist.targetY}`);
    assert.ok(waist.targetY > 1.0 && waist.targetY < 1.32, 'aimed at her chest');
  }
  const full = cameraFraming({ framing: 'full', aspect: 390 / 620, headY: HEAD_Y });
  const waist = cameraFraming({ framing: 'waist', aspect: 390 / 620, headY: HEAD_Y });
  assert.ok(full.dist * Math.tan(full.fov * Math.PI / 360) > 1.8 * waist.dist * Math.tan(waist.fov * Math.PI / 360),
    'she is at least 1.8× larger than in the old full-body embed');
});

test('waist limits: closer and wider than the standalone stage; the standalone limits are unchanged', () => {
  const standard = cameraLimits('full');
  assert.deepEqual(standard.zoom, CAMERA_ZOOM_RANGE);
  assert.deepEqual(cameraLimits('upper'), standard);
  assert.deepEqual(standard.pitch, { min: -0.22, max: 0.12 });
  assert.deepEqual(standard.panY, { min: -1.2, max: 1.2 });
  assert.equal(cameraLimits('waist'), WAIST_LIMITS);
  assert.ok(WAIST_LIMITS.panY.min < -1.2 && WAIST_LIMITS.panY.max > 1.2, 'drag ↕ reaches her feet or above her cap');
  assert.ok(WAIST_LIMITS.zoom.min > CAMERA_ZOOM_RANGE.min, 'pinch stops at a face close-up, not inside her face');
  assert.ok(WAIST_LIMITS.zoom.max >= 3);

  const view = createCameraView({ zoom: 0.01, panY: 9, dPhi: 4 }, WAIST_LIMITS);
  assert.deepEqual(view.state, { dTheta: 0, dPhi: WAIST_LIMITS.pitch.max, zoom: WAIST_LIMITS.zoom.min, panY: WAIST_LIMITS.panY.max });
  view.moveBy(10);
  close(view.state.panY, WAIST_LIMITS.panY.min);
  view.zoomBy(1000);
  close(view.state.zoom, WAIST_LIMITS.zoom.max);
  const placed = view.place({ radius: 2, phi: 1.6, theta: 0 });
  close(placed.minDistance, 2 * WAIST_LIMITS.zoom.min);
  close(placed.maxDistance, 2 * WAIST_LIMITS.zoom.max);
  view.reset();
  assert.deepEqual(view.state, { dTheta: 0, dPhi: 0, zoom: 1, panY: 0 });
  // No limits argument: the original stage ranges.
  const plain = createCameraView({ zoom: 0.01, panY: 9 });
  assert.deepEqual(plain.state, { dTheta: 0, dPhi: 0, zoom: CAMERA_ZOOM_RANGE.min, panY: 1.2 });
});

test('pinch spans a face close-up to her whole body in the embedded view', () => {
  const width = 390, height = 620;
  // Pinched all the way in: her face fills the frame, centred.
  const near = place({ width, height, view: { zoom: WAIST_LIMITS.zoom.min } });
  const face = HEAD_Y + WAIST_AIM.faceAbove;
  const faceY = near.screenY(face);
  assert.ok(Math.abs(faceY - height / 2) < height * 0.1, `face centred (at ${faceY.toFixed(0)} px)`);
  assert.ok(near.screenY(HEAD_Y - 0.12) - near.screenY(HEAD_Y + 0.18) > height * 0.7, 'her head fills most of the frame');
  assert.ok(near.camera.position.distanceTo(new Vector3(0, face, 0)) > 0.5, 'the camera stays outside her face');
  // Pinched out: the whole of her, cap to shoes, inside the frame.
  const fullZoom = 2.2;
  assert.ok(fullZoom <= WAIST_LIMITS.zoom.max);
  const far = place({ width, height, view: { zoom: fullZoom } });
  assert.ok(far.screenY(HEAD_Y + 0.2) > 0, 'cap in frame');
  assert.ok(far.screenY(0) < height, 'shoes in frame');
  // Every zoom in between keeps her in view (no jump through empty space).
  for (let zoom = WAIST_LIMITS.zoom.min; zoom <= WAIST_LIMITS.zoom.max; zoom *= 1.08) {
    const { screenY } = place({ width, height, view: { zoom } });
    assert.ok(screenY(face) > 0 && screenY(face) < height, `zoom ${zoom.toFixed(2)}: her face is on screen`);
  }
});

test('the zoom aim rises to her face pinching in and settles on her middle pinching out', () => {
  const targetY = cameraFraming({ framing: 'waist', aspect: 390 / 620, headY: HEAD_Y }).targetY;
  const aim = (zoom, extra = {}) => zoomAim({ framing: 'waist', zoom, targetY, headY: HEAD_Y, ...extra });
  close(aim(1), 0);
  assert.ok(Math.abs(aim(1.01)) < 1e-3 && Math.abs(aim(0.99)) < 1e-3, 'smooth through the default view');
  close(targetY + aim(WAIST_LIMITS.zoom.min), HEAD_Y + WAIST_AIM.faceAbove, 1e-9);
  assert.ok(WAIST_LIMITS.zoom.min >= 0.3 && WAIST_LIMITS.zoom.min <= 0.45);
  assert.ok(targetY + aim(2.2) < 0.9 && targetY + aim(2.2) > 0.7, 'her middle when the whole body shows');
  close(aim(3), aim(2.5), 1e-9);
  let previous = Infinity;
  for (let zoom = WAIST_LIMITS.zoom.min; zoom <= 3; zoom += 0.01) {
    const value = aim(zoom);
    assert.ok(value <= previous + 1e-12, `monotone at ${zoom.toFixed(2)}`);
    previous = value;
  }
  // Only the embedded framing aims; asleep she is framed from the side.
  for (const framing of ['full', 'upper']) assert.equal(zoomAim({ framing, zoom: 0.4, targetY, headY: HEAD_Y }), 0);
  assert.equal(aim(0.4, { state: 'sleep' }), 0);
  assert.equal(aim(Number.NaN), 0);
  assert.equal(aim(0.5, { headY: Number.NaN }), 0);
});

test('a view blend takes the short way round and eases zoom geometrically', () => {
  const from = { dTheta: 3, dPhi: 0.1, zoom: 0.5, panY: -0.4 };
  const to = { dTheta: -3, dPhi: 0, zoom: 2, panY: 0 };
  assert.deepEqual(blendView(from, to, 0), from);
  const end = blendView(from, to, 1);
  close(end.dTheta, -3); close(end.dPhi, 0); close(end.zoom, 2); close(end.panY, 0);
  const mid = blendView(from, to, 0.5);
  close(Math.abs(mid.dTheta), Math.PI, 1e-9);
  close(mid.zoom, 1);
  close(mid.panY, -0.2);
  const junk = blendView({ dTheta: Number.NaN, zoom: 0 }, { zoom: -1, panY: Infinity }, 0.5);
  assert.ok(Object.values(junk).every(Number.isFinite));
  assert.ok(junk.zoom > 0);
});

test('the reset tween eases out to the default view and reports when it is done', () => {
  const from = { dTheta: 1.2, dPhi: 0, zoom: 2.4, panY: 0.6 };
  const tween = createViewTween({ from, to: { dTheta: 0, dPhi: 0, zoom: 1, panY: 0 }, at: 1000, ms: 400 });
  assert.deepEqual(tween.value(1000), { view: from, done: false });
  assert.deepEqual(tween.value(900).view, from, 'before it starts');
  const half = tween.value(1200);
  assert.equal(half.done, false);
  assert.ok(half.view.dTheta < 1.2 * 0.5, 'ease-out: past halfway at half time');
  const end = tween.value(1400);
  assert.equal(end.done, true);
  assert.deepEqual(end.view, { dTheta: 0, dPhi: 0, zoom: 1, panY: 0 });
  assert.deepEqual(tween.value(5000), end);
  let last = Infinity;
  for (let t = 1000; t <= 1400; t += 10) {
    const { view } = tween.value(t);
    assert.ok(view.dTheta <= last + 1e-12);
    last = view.dTheta;
  }
});

test('a double tap is a second press soon after the first release, close by; a third tap starts over', () => {
  const tap = createDoubleTap({ ms: 350, slop: 36 });
  assert.equal(tap.tap(100, 100, 0, 120), false);
  assert.equal(tap.tap(110, 105, 400, 480), true, 'pressed 280 ms after the first release');
  assert.equal(tap.tap(110, 105, 600, 650), false, 'a third tap is a new first tap');
  assert.equal(tap.tap(112, 100, 1100, 1150), false, 'too slow');
  assert.equal(tap.tap(200, 100, 1300, 1350), false, 'too far from the last one');
  tap.reset();
  assert.equal(tap.tap(200, 100, 1400, 1450), false, 'reset forgets the pending tap');
  assert.equal(tap.tap(201, 101, 1500), true, 'release time defaults to the press time');
  assert.equal(tap.tap(Number.NaN, 0, 1600), false);
  assert.equal(tap.tap(0, 0, 1700, 1750), false);
  assert.equal(tap.tap(0, 0, 1000, 1010), false, 'a press from before the first one is not a double tap');
});

test('a tap reports when it was pressed and released (for the double tap)', async () => {
  const { createAvatarGesture } = await import('../../../flutter_app/web/companion/avatar/camera.js');
  const gesture = createAvatarGesture();
  gesture.down(1, 50, 50, 1000);
  assert.deepEqual(gesture.up(1, 52, 51, 1090), { x: 52, y: 51, downAt: 1000, upAt: 1090 });
});

test('the contact shadow shows whenever her feet can be on screen', () => {
  assert.equal(contactShadowWanted({ framing: 'full' }), true);
  assert.equal(contactShadowWanted({ framing: 'upper' }), false);
  assert.equal(contactShadowWanted({ framing: 'upper', state: 'sleep' }), true);
  assert.equal(contactShadowWanted({ framing: 'waist', zoom: 1 }), false);
  assert.equal(contactShadowWanted({ framing: 'waist', zoom: 2.2 }), true);
});

test('pinched in, the camera follows her head (quicker) so a face close-up keeps her face in frame', () => {
  const sway = { hipsX: 0.1, headX: 0.12 };
  // The standalone framings and the default embedded view trail her hips, as before.
  for (const framing of ['full', 'upper']) assert.deepEqual(followGoal({ framing, zoom: 0.4, ...sway }), { x: 0.085, rate: 0.8 });
  assert.deepEqual(followGoal({ framing: 'waist', zoom: 1, ...sway }), { x: 0.085, rate: 0.8 });
  assert.deepEqual(followGoal({ framing: 'waist', zoom: 2.5, ...sway }), { x: 0.085, rate: 0.8 });
  const near = followGoal({ framing: 'waist', zoom: WAIST_LIMITS.zoom.min, ...sway });
  close(near.x, 0.12);
  assert.ok(near.rate >= 3, 'fast enough to keep up with the idle sway');
  const mid = followGoal({ framing: 'waist', zoom: 0.68, ...sway });
  assert.ok(mid.x > 0.085 && mid.x < 0.12 && mid.rate > 0.8 && mid.rate < near.rate);
  assert.deepEqual(followGoal({ framing: 'waist', zoom: 0.4, hipsX: 0.1 }), { x: 0.085, rate: 0.8 }, 'no head bone: hips');
  assert.deepEqual(followGoal({ framing: 'waist', state: 'sleep', zoom: 0.4, ...sway }), { x: 0.085, rate: 0.8 });
  assert.deepEqual(followGoal({ framing: 'waist', zoom: 0.4, hipsX: Number.NaN, headX: Number.NaN }), { x: 0, rate: 0.8 });
});
