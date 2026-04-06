import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
THREE.GLTFLoader = GLTFLoader;
window.THREE = THREE;
window.__threeReady = true;
console.log('[three_loader] Three.js ready');
