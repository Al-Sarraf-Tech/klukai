// Her GLB is fetched once per model id per page: the boot warm-up and every
// createAvatar3D() share one download (and its ArrayBuffer), in any order.
// Under node there is no WebGL, so each viewer stops after the download
// (ready === false): exactly the part these tests count.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createAvatar3D, warmAvatar3D, forgetAvatarModels } from '../../../flutter_app/web/companion/avatar3d.js';

function counter() {
  const signals = [];
  const load = (signal) => { signals.push(signal); return Promise.resolve(new ArrayBuffer(8)); };
  return { load, signals, get calls() { return signals.length; } };
}
const quiet = () => {};

test('a stage load that starts before the warm-up still fetches once', async () => {
  // The embedded stage's boot order: setMode("model") builds the viewer,
  // then the warm-up runs. The warm-up used to start a second download.
  const c = counter();
  const viewer = createAvatar3D(null, { loadModel: c.load, modelKey: 'm-load-first', onError: quiet });
  warmAvatar3D({ loadModel: c.load, modelKey: 'm-load-first' });
  assert.equal((await viewer).ready, false);
  assert.equal(c.calls, 1);
  forgetAvatarModels();
});

test('the warm-up download is the one the stage load uses', async () => {
  const c = counter();
  warmAvatar3D({ loadModel: c.load, modelKey: 'm-warm-first' });
  warmAvatar3D({ loadModel: c.load, modelKey: 'm-warm-first' });
  await createAvatar3D(null, { loadModel: c.load, modelKey: 'm-warm-first', onError: quiet });
  assert.equal(c.calls, 1);
  forgetAvatarModels();
});

test('a retry, or a second viewer of the same model, reuses the downloaded bytes', async () => {
  const c = counter();
  await createAvatar3D(null, { loadModel: c.load, modelKey: 'm-retry', onError: quiet });
  await createAvatar3D(null, { loadModel: c.load, modelKey: 'm-retry', onError: quiet });
  warmAvatar3D({ loadModel: c.load, modelKey: 'm-retry' }); // late warm-up: nothing to do
  assert.equal(c.calls, 1);
  forgetAvatarModels(); // his session ended
  await createAvatar3D(null, { loadModel: c.load, modelKey: 'm-retry', onError: quiet });
  assert.equal(c.calls, 2);
  forgetAvatarModels();
});

test('another appearance is its own download and cancels the one it replaces', async () => {
  const c = counter();
  let release;
  const slow = (signal) => { c.signals.push(signal); return new Promise((resolve) => { release = resolve; }); };
  warmAvatar3D({ loadModel: slow, modelKey: 'm-old' });
  await Promise.resolve();
  await createAvatar3D(null, { loadModel: c.load, modelKey: 'm-new', onError: quiet });
  assert.equal(c.calls, 2);
  assert.equal(c.signals[0].aborted, true, 'the superseded download is aborted');
  assert.equal(c.signals[1].aborted, false);
  release(new ArrayBuffer(8));
  forgetAvatarModels();
});

test('a failed download is not kept: the next load fetches again', async () => {
  let calls = 0;
  const flaky = () => (++calls === 1 ? Promise.reject(new Error('offline')) : Promise.resolve(new ArrayBuffer(8)));
  const errors = [];
  await createAvatar3D(null, { loadModel: flaky, modelKey: 'm-flaky', onError: (e) => errors.push(e.message) });
  assert.deepEqual(errors, ['offline']);
  await createAvatar3D(null, { loadModel: flaky, modelKey: 'm-flaky', onError: quiet });
  assert.equal(calls, 2);
  forgetAvatarModels();
});

test('a caller giving up stops waiting without cancelling the shared download', async () => {
  const c = counter();
  let release;
  const slow = (signal) => { c.signals.push(signal); return new Promise((resolve) => { release = resolve; }); };
  const giveUp = new AbortController();
  const first = createAvatar3D(null, { loadModel: slow, modelKey: 'm-abort', signal: giveUp.signal, onError: quiet });
  await Promise.resolve();
  giveUp.abort();
  assert.equal((await first).ready, false);
  assert.equal(c.signals[0].aborted, false);
  release(new ArrayBuffer(8));
  await createAvatar3D(null, { loadModel: c.load, modelKey: 'm-abort', onError: quiet });
  assert.equal(c.calls, 1);
  forgetAvatarModels();
});
