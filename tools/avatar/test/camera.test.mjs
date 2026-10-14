import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PerspectiveCamera, Spherical, Vector3 } from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import * as avatar from '../../../flutter_app/web/companion/avatar3d.js';

const base = { radius: 10, phi: 1.5, theta: 0 };
const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-10, `${actual} ≠ ${expected}`);
function view() {
  assert.equal(typeof avatar.createCameraView, 'function', 'persistent camera view is available');
  return avatar.createCameraView();
}

test('short landscape framing keeps the full avatar above a dock occupying over half the screen', () => {
  const width = 568, height = 320, top = 48, bottom = 181.4;
  const placement = avatar.cameraFraming({ framing: 'full', aspect: width / height,
    insets: { top: top / height, bottom: bottom / height } });
  const camera = new PerspectiveCamera(placement.fov, width / height, 0.05, 30);
  camera.position.set(0, placement.camY, placement.dist);
  camera.lookAt(0, placement.targetY, 0);
  camera.updateMatrixWorld();
  const screenY = (y) => (1 - new Vector3(0, y, 0).project(camera).y) * height / 2;
  assert.ok(screenY(1.54) >= top, 'cap clears the actual topbar');
  assert.ok(screenY(0) < height - bottom - 8, `feet at ${screenY(0)} must clear dock at ${height - bottom}`);
  assert.ok(screenY(0) - screenY(1.54) > 45, 'the usable band still shows a legible full body');
});

test('overlapping or invalid camera insets still produce a finite usable projection', () => {
  for (const insets of [{ top: 0.9, bottom: 0.9 }, { top: 100, bottom: 100 }, { top: Infinity, bottom: NaN }]) {
    const f = avatar.cameraFraming({ framing: 'full', aspect: 568 / 320, insets });
    assert.ok([f.camY, f.targetY, f.dist].every(Number.isFinite));
    assert.ok(f.dist > 0 && f.dist < 30, 'minimum usable band keeps the avatar within the renderer far plane');
    const camera = new PerspectiveCamera(f.fov, 568 / 320, 0.05, 30);
    const target = new Vector3(0, f.targetY, 0);
    const offset = new Vector3(0, f.camY - f.targetY, f.dist);
    const spherical = new Spherical().setFromVector3(offset);
    const placed = avatar.createCameraView({ zoom: 1.8 }).place(spherical);
    camera.position.copy(target).add(offset.setFromSpherical(spherical.set(placed.radius, placed.phi, placed.theta)));
    camera.lookAt(target); camera.updateMatrixWorld();
    const body = new Vector3(0, 0.8, 0).project(camera);
    assert.ok(body.z > -1 && body.z < 1, 'even maximum user zoom keeps the body inside the near/far clipping planes');
  }
});

test('camera buttons retain zoom limits while rotating past the back of the avatar', () => {
  const camera = view();
  camera.zoomBy(0.8);
  camera.rotateBy(Math.PI / 4);
  let placed = camera.place(base);
  close(placed.radius, 8);
  close(placed.theta, Math.PI / 4);
  camera.zoomBy(0.01);
  camera.rotateBy(Math.PI);
  placed = camera.place(base);
  close(placed.radius, 6);
  close(placed.theta, -3 * Math.PI / 4);
  camera.zoomBy(100);
  camera.rotateBy(-Math.PI);
  placed = camera.place(base);
  close(placed.radius, 18);
  close(placed.theta, Math.PI / 4);
});

test('repeated button turns normalize yaw without losing the chosen zoom or pitch', () => {
  const camera = view();
  camera.place(base);
  camera.absorb({ radius: 8, theta: Math.PI / 6, phi: 1.6 });
  camera.place(base);
  for (const direction of [1, -1]) {
    for (let turn = 0; turn < 240; turn++) {
      camera.rotateBy(direction * Math.PI / 12);
      assert.ok(Math.abs(camera.state.dTheta) <= Math.PI);
    }
    const placed = camera.place(base);
    close(placed.theta, Math.PI / 6);
    close(placed.radius, 8);
    close(placed.phi, 1.6);
  }
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

test('crossing the spherical angle wrap preserves an offset relative to framing', () => {
  const camera = view();
  camera.rotateBy(1.5);
  camera.place({ ...base, theta: 1.6 });
  camera.absorb({ radius: 10, phi: 1.5, theta: 3.2 - 2 * Math.PI });
  close(camera.state.dTheta, 1.6);
});

test('pointer motion crosses the rear seam continuously in both directions', () => {
  const camera = view();
  camera.rotateBy(3.1);
  camera.place(base);
  camera.absorb({ radius: 10, phi: 1.5, theta: -3.1 });
  close(camera.state.dTheta, -3.1);
  camera.place(base);
  camera.absorb({ radius: 10, phi: 1.5, theta: 3.1 });
  close(camera.state.dTheta, 3.1);
});

test('camera bounds allow real OrbitControls to make repeated full turns in either direction', () => {
  const viewState = view();
  const camera = new PerspectiveCamera();
  const spherical = new Spherical();
  const offset = new Vector3();
  const controls = new OrbitControls(camera);
  controls.enableDamping = false;
  const place = () => {
    const placed = viewState.place(base);
    camera.position.setFromSpherical(spherical.set(placed.radius, placed.phi, placed.theta));
    controls.minAzimuthAngle = placed.minAzimuthAngle;
    controls.maxAzimuthAngle = placed.maxAzimuthAngle;
    controls.update();
  };
  place();
  for (const direction of [1, -1]) {
    for (let quarter = 1; quarter <= 12; quarter++) {
      controls.rotateLeft(-direction * Math.PI / 2);
      viewState.absorb(spherical.setFromVector3(offset.subVectors(camera.position, controls.target)));
      place();
      const expected = direction * quarter * Math.PI / 2;
      close(Math.sin(viewState.state.dTheta), Math.sin(expected));
      close(Math.cos(viewState.state.dTheta), Math.cos(expected));
      assert.ok(Math.abs(viewState.state.dTheta) <= Math.PI);
      close(viewState.state.zoom, 1);
    }
  }
});

test('pointer changes respect zoom and polar limits while preserving valid state', () => {
  const camera = view();
  camera.place(base);
  camera.absorb({ radius: 0.01, theta: 2.5, phi: 3 });
  let placed = camera.place(base);
  close(placed.radius, 6);
  close(placed.theta, 2.5);
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
