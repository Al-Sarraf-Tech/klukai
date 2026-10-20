import { createModelCache } from '../appearance.js';

export let libsPromise = null;
export function importLibs(withOrbit = true) {
  if (!libsPromise) {
    // One tree-shaken bundle (tools/avatar/vendor.mjs): three core subset +
    // GLTFLoader + MeshoptDecoder + OrbitControls.
    libsPromise = import('../vendor/three-avatar.min.js?v=ba766fd1f3da')
      .then((m) => ({ THREE: m, GLTFLoader: m.GLTFLoader, MeshoptDecoder: m.MeshoptDecoder, OrbitControls: m.OrbitControls }));
    libsPromise.catch(() => { libsPromise = null; });
  }
  void withOrbit;
  return libsPromise;
}

// Every createAvatar3D() and warmAvatar3D() on this page shares it: one
// download per model id per page, whatever order they run in.
export const modelCache = createModelCache();
/**
 * Optional: call as soon as the 3D window is the selected mode (e.g. at page
 * init when the saved preference is 3D) to start the three.js imports and the
 * model download while the rest of the page boots. The next createAvatar3D()
 * with the same modelKey reuses both, and a warm-up for a model that is
 * already loading or loaded does nothing. Safe to call repeatedly; never throws.
 */
export function warmAvatar3D({ loadModel, modelKey = 'default' } = {}) {
  importLibs().catch(() => {});
  if (typeof loadModel === 'function') modelCache.warm(modelKey, loadModel);
}

/** Forget the downloaded model (his session ended): the next load fetches again. */
export function forgetAvatarModels() {
  modelCache.clear();
}
