import { OUTLINE, OUTLINE_FRAG_CUT, OUTLINE_FRAG_PARS, OUTLINE_VERT_PARS, OUTLINE_VERT_PROJECT, SHADE, SHADOW_FRAG, SHADOW_VERT, SWAY_VERT_PARS, TOON_FRAG_AO, TOON_FRAG_FX, TOON_FRAG_PARS, TOON_LIGHTS, TOON_VERT_NORMAL, TOON_VERT_PARS, TOON_VERT_SWAY } from './shaders.js';
import { smoothstep } from './common.js';
import { speechMouthDelta, speechMouthLevel } from './presence.js';

// Materials operations on the single avatar runtime owner.
export function installMaterials(ctx) {
// the GLB carries hair bone chains (spring physics) → no vertex sway
  ctx.toonFrom = function toonFrom(src, role) {
    if (role === 'overlay') {
      return new ctx.THREE.MeshBasicMaterial({
        name: src.name, map: src.map, transparent: true, blending: ctx.THREE.AdditiveBlending, depthWrite: false,
      });
    }
    const p = SHADE[role] || SHADE.cloth;
    const m = new ctx.THREE.MeshToonMaterial({
      name: src.name,
      map: src.map || null,
      color: src.map ? 0xffffff : src.color,
      side: src.side,
    });
    // No polygonOffset: on double-sided cloth it is slope-scaled and GPU-
    // dependent (Safari/Metal applies far more bias), which pulled the jacket's
    // inner/back layers through the front — she looked "hollow". Skin that
    // z-fought under clothes is removed/shrunk in the build instead.
    const u = {
      uShadowTint: { value: new ctx.THREE.Vector3(...p.tint) }, uTh: { value: p.th }, uSoft: { value: p.soft },
      uRimMul: { value: p.rim },
    };
    const defs = {};
    const sameUv = (t) => t && src.map && (t.channel ?? 0) === (src.map.channel ?? 0);
    if (role === 'face' && ctx.hasHeadCenter && !/eye/i.test(src.name)) defs.KL_FACE = ''; // not the iris discs
    if (role === 'hair') { if (ctx.hasHeadCenter) defs.KL_RING = ''; if (!ctx.hasChains) defs.KL_SWAY = ''; } // chains: real bones
    if (sameUv(src.aoMap)) { defs.KL_AO = ''; u.uAoMap = { value: src.aoMap }; ctx.ownedTextures.add(src.aoMap); }
    if (role === 'cloth' && sameUv(src.metalnessMap)) {
      defs.KL_METAL = ''; u.uMetalMap = { value: src.metalnessMap }; ctx.ownedTextures.add(src.metalnessMap);
    }
    m.defines = { ...(m.defines || {}), ...defs };
    m.userData.klRole = role;
    m.userData.klUniforms = u;
    m.onBeforeCompile = (shader) => {
      Object.assign(shader.uniforms, ctx.shared, u);
      shader.vertexShader = shader.vertexShader
        .replace('#include <common>', `#include <common>\n${TOON_VERT_PARS}`)
        .replace('#include <beginnormal_vertex>', `#include <beginnormal_vertex>\n${TOON_VERT_NORMAL}`)
        .replace('#include <skinning_vertex>', `${TOON_VERT_SWAY}\n#include <skinning_vertex>`);
      shader.fragmentShader = shader.fragmentShader
        .replace('#include <common>', `#include <common>\n${TOON_FRAG_PARS}`)
        .replace('#include <lights_toon_pars_fragment>', TOON_LIGHTS)
        .replace('#include <map_fragment>', `#include <map_fragment>\n${TOON_FRAG_AO}`)
        .replace('#include <opaque_fragment>', `${TOON_FRAG_FX}\n#include <opaque_fragment>`);
    };
    // Same patch code for every toon part; the features differ only by defines,
    // which are part of the key (three adds them too), so programs never collide.
    m.customProgramCacheKey = () => `klukai-toon2:${Object.keys(m.defines).sort().join(',')}`;
    return m;
  };

// Head-sphere centre in the GLB's (quantized) mesh space, from the skin's
  // inverse bind matrices: Head joint + (0, 5.1, −2.5) cm when Head is skinned,
  // else Neck + 12 cm up (the same point). Never metres: positions are
  // int16-quantized (~0.78 m/unit).
  ctx.headCenterFromSkin = function headCenterFromSkin(sk) {
    const find = (n) => sk.bones.findIndex((b) => b.name === n);
    const hi = find('mixamorigHead');
    const ni = find('mixamorigNeck');
    const i = hi >= 0 ? hi : ni;
    if (i < 0) return null;
    const bw = sk.boneInverses[i].clone().invert(); // bone bind pose in mesh space
    const upm = bw.getMaxScaleOnAxis(); // mesh units per metre
    const c = new ctx.THREE.Vector3().setFromMatrixPosition(bw);
    if (hi >= 0) c.add(new ctx.THREE.Vector3(0, 0.051 * upm, -0.025 * upm)); else c.add(new ctx.THREE.Vector3(0, 0.12 * upm, 0));
    return { center: c, upm };
  };

// Smoothed normals for the hull where the build didn't bake `_outline`:
  // average normals of coincident positions across every outlined part (both
  // meshes share one quantized space), w = width mask.
  ctx.bakeOutlineNormals = function bakeOutlineNormals(meshes) {
    // quantized (normalized int8/16) positions → one exact numeric key, no string per vertex
    const scaleOf = (P) => {
      const arr = P.array || P.data?.array;
      if (!P.normalized) return 0;
      return arr instanceof Int16Array ? 32767 : arr instanceof Uint16Array ? 65535 : arr instanceof Int8Array ? 127 : arr instanceof Uint8Array ? 255 : 0;
    };
    const keyOf = (P, i, q) => {
      if (q) { // each component + 32768 fits in 17 bits → < 2^51, exact in a double
        const c = (v) => Math.round(v * q) + 32768;
        return (c(P.getX(i)) * 131072 + c(P.getY(i))) * 131072 + c(P.getZ(i));
      }
      return `${Math.round(P.getX(i) * 1e5)},${Math.round(P.getY(i) * 1e5)},${Math.round(P.getZ(i) * 1e5)}`;
    };
    const acc = new Map();
    for (const o of meshes) {
      const P = o.geometry.attributes.position; const N = o.geometry.attributes.normal;
      if (!N) continue;
      const q = scaleOf(P);
      for (let i = 0; i < P.count; i++) {
        const k = keyOf(P, i, q); const a = acc.get(k);
        const nx = N.getX(i); const ny = N.getY(i); const nz = N.getZ(i);
        if (a) { a[0] += nx; a[1] += ny; a[2] += nz; } else acc.set(k, [nx, ny, nz]);
      }
    }
    for (const o of meshes) {
      const g = o.geometry;
      if (g.getAttribute('_outline')) continue;
      const P = g.attributes.position; const N = g.attributes.normal;
      const out = new Float32Array(P.count * 4);
      const face = o.material.userData.klRole === 'face';
      const q = scaleOf(P);
      for (let i = 0; i < P.count; i++) {
        let nx = N ? N.getX(i) : 0; let ny = N ? N.getY(i) : 1; let nz = N ? N.getZ(i) : 0;
        const a = acc.get(keyOf(P, i, q));
        const len = a ? Math.hypot(a[0], a[1], a[2]) : 0;
        if (len > 0.3) { nx = a[0] / len; ny = a[1] / len; nz = a[2] / len; }
        // face: no hull lines on the front (nose, mouth corners, eye sockets), only on the contour
        const wMask = face ? 1 - smoothstep(0.55, 0.85, N ? N.getZ(i) : 0) : 1;
        out.set([nx, ny, nz, wMask], i * 4);
      }
      g.setAttribute('_outline', new ctx.THREE.BufferAttribute(out, 4));
    }
  };

  ctx.outlineMaterial = function outlineMaterial(src, role) {
    const p = OUTLINE[role];
    const color = new ctx.THREE.Color(p.tint);
    if (!src.map && src.color) color.multiply(src.color);
    const mat = new ctx.THREE.MeshBasicMaterial({ name: `${src.name}_outline`, map: src.map || null, color, side: ctx.THREE.BackSide });
    // discard only where needed: it costs early-z, so cloth and hair hulls stay without it
    if (role === 'skin' || role === 'face') mat.defines = { ...(mat.defines || {}), KL_FACING: '' };
    // the hair hull must sway with the hair it outlines, or its lines float beside the tips
    if (role === 'hair' && !ctx.hasChains) mat.defines = { ...(mat.defines || {}), KL_SWAY: '' };
    const u = { uPx: { value: p.px }, uZ: { value: p.z } };
    mat.userData.klUniforms = u;
    mat.onBeforeCompile = (shader) => {
      Object.assign(shader.uniforms, ctx.outlineU, u);
      if (role === 'hair') {
        for (const k of ['uTime', 'uSway', 'uSwayBox', 'uSwayAmp']) shader.uniforms[k] = ctx.shared[k];
      }
      shader.vertexShader = shader.vertexShader
        .replace('#include <common>', `#include <common>\n${OUTLINE_VERT_PARS}\n${SWAY_VERT_PARS}`)
        .replace('#include <beginnormal_vertex>', '#include <beginnormal_vertex>\nobjectNormal = normalize( _outline.xyz + vec3( 0.0, 1e-5, 0.0 ) );')
        .replace('#include <skinning_vertex>', `${TOON_VERT_SWAY}\n#include <skinning_vertex>`)
        .replace('#include <project_vertex>', `#include <project_vertex>\n${OUTLINE_VERT_PROJECT}`);
      shader.fragmentShader = shader.fragmentShader
        .replace('#include <common>', `#include <common>\n${OUTLINE_FRAG_PARS}`)
        .replace('#include <clipping_planes_fragment>', `${OUTLINE_FRAG_CUT}\n#include <clipping_planes_fragment>`);
    };
    mat.customProgramCacheKey = () => `klukai-outline4:${Object.keys(mat.defines || {}).sort().join(',')}`;
    return mat;
  };

  ctx.addOutlines = function addOutlines() {
    const list = ctx.skinned.filter((o) => {
      const mat = o.material;
      if (Array.isArray(mat) || !OUTLINE[mat.userData.klRole] || /eye/i.test(mat.name)) return false;
      // a flat near-black part (no texture) gains nothing from a dark hull: save the draw call
      return Boolean(mat.map) || mat.color.r + mat.color.g + mat.color.b > 0.06;
    });
    if (list.some((o) => !o.geometry.getAttribute('_outline'))) ctx.bakeOutlineNormals(list);
    for (const src of list) {
      const o = new ctx.THREE.SkinnedMesh(src.geometry, ctx.outlineMaterial(src.material, src.material.userData.klRole));
      o.name = `${src.name}_outline`;
      o.bind(src.skeleton, src.bindMatrix);
      o.position.copy(src.position); o.quaternion.copy(src.quaternion); o.scale.copy(src.scale);
      o.morphTargetInfluences = src.morphTargetInfluences; // same array → blink follows
      o.morphTargetDictionary = src.morphTargetDictionary;
      o.frustumCulled = false;
      o.renderOrder = 1; // after the opaques: early-z rejects hidden hull pixels
      o.visible = ctx.outlinesOn;
      o.userData.klOutline = true;
      o.userData.role = src.material.userData.klRole;
      src.parent.add(o);
      ctx.outlineMeshes.push(o);
    }
  };

  ctx.setOutlines = function setOutlines(on) {
    ctx.outlinesOn = Boolean(on) && ctx.outlineMeshes.length > 0;
    for (const o of ctx.outlineMeshes) o.visible = ctx.outlinesOn;
  };

  ctx.buildSpeechMouth = function buildSpeechMouth() {
    ctx.root.updateMatrixWorld(true);
    for (const mesh of ctx.skinned) {
      const dict = mesh.morphTargetDictionary || {};
      const authored = Object.keys(dict).find((n) => /^(jaw_?open|mouth_?open|viseme_?aa|aa)$/i.test(n));
      if (authored) {
        ctx.mouthTargets.push({ mesh, index: dict[authored], rest: mesh.morphTargetInfluences[dict[authored]] || 0 });
        ctx.mouthMode = 'morph';
        continue;
      }
      // The installed default + Garf appearances share this face family. Their
      // decoded bind-space lips were measured separately before calibration.
      const faceAtlas = mesh.material?.name;
      if (!['c_Clukay_face_d', 'Klukai Face'].includes(faceAtlas)) continue;
      const p = mesh.geometry.attributes.position;
      if (!p || typeof mesh.applyBoneTransform !== 'function') continue;
      const delta = new Float32Array(p.count * 3);
      const v = new ctx.THREE.Vector3();
      let moved = 0;
      for (let i = 0; i < p.count; i++) {
        v.fromBufferAttribute(p, i);
        mesh.applyBoneTransform(i, v);
        const offset = speechMouthDelta(v.toArray(), faceAtlas === 'Klukai Face' ? 1.33745 : 1.3365);
        if (offset[1]) moved++;
        for (let axis = 0; axis < 3; axis++) {
          delta[i * 3 + axis] = offset[axis] * ctx.unitsPerMetre;
          if (!mesh.geometry.morphTargetsRelative) delta[i * 3 + axis] += [p.getX(i), p.getY(i), p.getZ(i)][axis];
        }
      }
      if (!moved) continue;
      const attrs = mesh.geometry.morphAttributes;
      const positions = attrs.position || (attrs.position = []);
      const index = positions.length;
      const attribute = new ctx.THREE.BufferAttribute(delta, 3);
      attribute.name = 'speechMouth';
      positions.push(attribute);
      if (attrs.normal?.length) {
        const normals = mesh.geometry.morphTargetsRelative ? new Float32Array(delta.length) : Float32Array.from(mesh.geometry.attributes.normal.array);
        attrs.normal.push(new ctx.THREE.BufferAttribute(normals, 3));
      }
      const previous = mesh.morphTargetInfluences?.slice() || [];
      mesh.updateMorphTargets();
      mesh.morphTargetDictionary = { ...dict, speechMouth: index };
      previous.forEach((value, i) => { mesh.morphTargetInfluences[i] = value; });
      ctx.mouthTargets.push({ mesh, index, rest: 0 });
      if (ctx.mouthMode !== 'morph') ctx.mouthMode = 'surface';
    }
  };

  ctx.applyMouth = function applyMouth(dt) {
    const target = ctx.stateName === 'sleep' ? 0 : speechMouthLevel(ctx.speakingTarget);
    ctx.mouthOpen += (target - ctx.mouthOpen) * (1 - Math.exp(-dt / (target > ctx.mouthOpen ? 0.035 : 0.075)));
    if (target === 0 && ctx.mouthOpen < 0.001) ctx.mouthOpen = 0;
    for (const m of ctx.mouthTargets) m.mesh.morphTargetInfluences[m.index] = m.rest + (1 - m.rest) * ctx.mouthOpen;
  };

// Hair sway bounds from all hair geometry together (one shared uniform; quantized units, never metres).
  ctx.setSwayBox = function setSwayBox(geos) {
    const bb = new ctx.THREE.Box3();
    for (const g of geos) {
      if (!g.boundingBox) g.computeBoundingBox();
      if (g.boundingBox) bb.union(g.boundingBox);
    }
    if (bb.isEmpty() || !Number.isFinite(bb.min.y)) return;
    const hcZ = ctx.shared.uHeadCenter.value.z;
    const upm = ctx.unitsPerMetre;
    // sway starts at about neck height and is full at the tips; z: only hair behind her back
    ctx.shared.uSwayBox.value.set(bb.min.y + 0.67 * (bb.max.y - bb.min.y), bb.min.y, hcZ - 0.12 * upm, hcZ - 0.2 * upm);
  };

  ctx.makeContactShadow = function makeContactShadow() {
    const mat = new ctx.THREE.ShaderMaterial({
      uniforms: {
        uHip: { value: new ctx.THREE.Vector4() }, uFeet: { value: new ctx.THREE.Vector4() }, uW: { value: new ctx.THREE.Vector4() },
        uColor: { value: new ctx.THREE.Color(0x05070c) }, uAlpha: { value: 0 },
      },
      vertexShader: SHADOW_VERT, fragmentShader: SHADOW_FRAG, transparent: true, depthWrite: false,
    });
    const m = new ctx.THREE.Mesh(new ctx.THREE.PlaneGeometry(2.6, 2.6), mat);
    m.rotation.x = -Math.PI / 2;
    m.position.y = 0.002;
    m.frustumCulled = false;
    m.visible = false;
    m.name = 'klukai_contact_shadow';
    ctx.scene.add(m);
    return m;
  };

  ctx.updateShadow = function updateShadow(dt) {
    if (!ctx.shadowMesh || !ctx.bones.Hips) return;
    const want = ctx.framing === 'full' || ctx.stateName === 'sleep' ? 1 : 0;
    ctx.shadowA += (want - ctx.shadowA) * (1 - Math.exp(-dt * 3));
    ctx.shadowMesh.visible = ctx.shadowA > 0.01;
    if (!ctx.shadowMesh.visible) return;
    const u = ctx.shadowMesh.material.uniforms;
    const hp = ctx.bones.Hips.getWorldPosition(ctx._v1);
    const cp = (ctx.bones.Spine2 || ctx.bones.Hips).getWorldPosition(ctx._v2);
    u.uHip.value.set(hp.x, hp.z, cp.x, cp.z);
    const low = (y, a, b) => 1 - smoothstep(a, b, y);
    const lf = (ctx.bones.LeftToeBase || ctx.bones.LeftFoot)?.getWorldPosition(ctx._f1);
    const rf = (ctx.bones.RightToeBase || ctx.bones.RightFoot)?.getWorldPosition(ctx._f2);
    u.uFeet.value.set(lf?.x ?? 0, lf?.z ?? 0, rf?.x ?? 0, rf?.z ?? 0);
    u.uW.value.set(0.3, 0.35 * low(cp.y, 0.2, 0.6), lf ? 0.45 * low(lf.y, 0.04, 0.14) : 0, rf ? 0.45 * low(rf.y, 0.04, 0.14) : 0);
    u.uAlpha.value = ctx.shadowA;
    ctx.shadowMesh.position.x = hp.x; ctx.shadowMesh.position.z = hp.z;
  };

// Mood rim colour (and ≤10 % of the sky fill) eases toward setTint().
  ctx.updateTint = function updateTint(dt) {
    const k = 1 - Math.exp(-dt * 3.2); // ~1.2 s, like the stage's CSS background transition
    ctx.shared.uRimColor.value.lerp(ctx.rimGoal, k);
    ctx.hemi.color.copy(ctx.skyBase).lerp(ctx.shared.uRimColor.value, 0.08);
    ctx.camera.updateMatrixWorld();
    ctx.shared.uRimDir.value.copy(ctx.rimDirWorld).transformDirection(ctx.camera.matrixWorldInverse);
    ctx._v1.copy(ctx.key.position).sub(ctx.key.target.position).transformDirection(ctx.camera.matrixWorldInverse);
    ctx.shared.uKeySide.value.set(ctx._v1.x, ctx._v1.y).normalize();
  };
}
