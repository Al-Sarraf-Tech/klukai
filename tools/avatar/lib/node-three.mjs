// Minimal Node shims so three.js loaders/exporters run headless (no DOM, no canvas).
import * as THREE from 'three';

if (typeof globalThis.self === 'undefined') globalThis.self = globalThis;
if (typeof globalThis.window === 'undefined') globalThis.window = globalThis;

// GLTFExporter assembles the GLB through Blob + FileReader. Node has Blob only.
if (typeof globalThis.FileReader === 'undefined') {
  globalThis.FileReader = class FileReader {
    readAsArrayBuffer(blob) {
      blob.arrayBuffer().then((ab) => { this.result = ab; this.onload?.({ target: this }); this.onloadend?.({ target: this }); });
    }
    readAsDataURL(blob) {
      blob.arrayBuffer().then((ab) => {
        this.result = `data:${blob.type || 'application/octet-stream'};base64,${Buffer.from(ab).toString('base64')}`;
        this.onload?.({ target: this }); this.onloadend?.({ target: this });
      });
    }
  };
}

// Textures are attached later from assets/textures, so loaders never fetch images.
THREE.TextureLoader.prototype.load = function load(url) {
  const t = new THREE.Texture();
  t.userData = { url };
  return t;
};

export { THREE };
