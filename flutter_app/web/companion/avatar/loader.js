import { createModelPrefetch } from '../appearance.js';

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

export const modelPrefetch = createModelPrefetch();
/**
 * Optional: call as soon as the 3D window is the selected mode (e.g. at page
 * init when the saved preference is 3D) to start the three.js imports and the
 * model download while the rest of the page boots. The next createAvatar3D()
 * reuses both. Safe to call repeatedly; never throws.
 */
export function warmAvatar3D({ loadModel, modelKey = 'default' } = {}) {
  importLibs().catch(() => {});
  if (typeof loadModel === 'function') modelPrefetch.warm(modelKey, loadModel);
}
