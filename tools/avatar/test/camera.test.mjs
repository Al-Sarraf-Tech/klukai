import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as avatar from '../../../flutter_app/web/companion/avatar3d.js';

const base = { radius: 10, phi: 1.5, theta: 0 };
const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-10, `${actual} ≠ ${expected}`);
function view() {
  assert.equal(typeof avatar.createCameraView, 'function', 'persistent camera view is available');
  return avatar.createCameraView();
}

test('camera buttons clamp zoom and horizontal rotation to the usable inspection range', () => {
  const camera = view();
  camera.zoomBy(0.8);
  camera.rotateBy(Math.PI / 4);
  let placed = camera.place(base);
  close(placed.radius, 8);
  close(placed.theta, Math.PI / 4);
  camera.zoomBy(0.01);
  camera.rotateBy(10);
  placed = camera.place(base);
  close(placed.radius, 6);
  close(placed.theta, 2 * Math.PI / 3);
  camera.zoomBy(100);
  camera.rotateBy(-20);
  placed = camera.place(base);
  close(placed.radius, 18);
  close(placed.theta, -2 * Math.PI / 3);
});

test('pointer camera changes persist after release without an automatic springback', () => {
  const camera = view();
  camera.place(base);
  camera.absorb({ radius: 8, theta: 0.5, phi: 1.6 });
  for (let frame = 0; frame < 600; frame++) {
    const placed = camera.place(base);
    close(placed.radius, 8);
    close(placed.theta, 0.5);
    close(placed.phi, 1.6);
    camera.absorb(placed);
  }
});

test('automatic speech push-in and animated framing never compound into user zoom', () => {
  const camera = view();
  camera.zoomBy(0.8);
  camera.rotateBy(0.5);
  let placed = camera.place(base);
  for (let frame = 0; frame < 600; frame++) {
    camera.absorb(placed);
    const pushIn = frame % 2;
    placed = camera.place({ radius: 10 + frame / 100, phi: 1.4, theta: 0.2 }, pushIn);
  }
  close(camera.state.zoom, 0.8);
  close(camera.state.dTheta, 0.5);
  close(camera.place(base).radius, 8);
});

test('resize and sleep framing retain zoom and orbit relative to the new frame', () => {
  const camera = view();
  camera.place(base);
  camera.absorb({ radius: 7, theta: -0.8, phi: 1.4 });
  camera.place(base);
  const placed = camera.place({ radius: 5, theta: Math.PI / 2, phi: 1.2 });
  close(placed.radius, 3.5);
  close(placed.theta, Math.PI / 2 - 0.8);
  close(placed.phi, 1.1);
});

test('crossing the spherical angle wrap does not jump to the opposite orbit limit', () => {
  const camera = view();
  camera.rotateBy(1.5);
  camera.place({ ...base, theta: 1.6 });
  camera.absorb({ radius: 10, phi: 1.5, theta: 3.2 - 2 * Math.PI });
  close(camera.state.dTheta, 1.6);
});

test('pointer changes respect zoom and polar limits while preserving valid state', () => {
  const camera = view();
  camera.place(base);
  camera.absorb({ radius: 0.01, theta: 2.5, phi: 3 });
  let placed = camera.place(base);
  close(placed.radius, 6);
  close(placed.theta, 2 * Math.PI / 3);
  close(placed.phi, 1.62);
  camera.absorb({ radius: 1000, theta: -1, phi: -1 });
  placed = camera.place(base);
  close(placed.radius, 18);
  close(placed.phi, 1.28);
});

test('reset restores the current automatic view and invalid button arguments are inert', () => {
  const camera = view();
  camera.zoomBy(0.7);
  camera.rotateBy(1);
  camera.place(base);
  camera.reset();
  for (const invalid of [NaN, Infinity, -Infinity, undefined, 'oops']) {
    camera.zoomBy(invalid);
    camera.rotateBy(invalid);
  }
  camera.zoomBy(0);
  camera.zoomBy(-1);
  const placed = camera.place({ radius: 5, phi: 1.4, theta: 1 });
  close(placed.radius, 5);
  close(placed.phi, 1.4);
  close(placed.theta, 1);
});

test('camera control methods remain safe when the model cannot be created', async () => {
  const failed = await avatar.createAvatar3D({});
  for (const name of ['zoomBy', 'rotateBy', 'resetView']) {
    assert.equal(typeof failed[name], 'function', name);
    assert.doesNotThrow(() => failed[name](0.8));
  }
});
