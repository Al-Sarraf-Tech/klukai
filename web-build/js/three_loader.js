import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
window.THREE = THREE;
window.GLTFLoader = GLTFLoader;
window.__threeReady = true;
console.log('[three_loader] Three.js ready');
