

export const ROLE_FALLBACK = (name) => (/hair/i.test(name) ? 'hair' : /face|eye/i.test(name) ? 'face' : /cloth/i.test(name) ? 'cloth' : 'skin');

// ── look constants (tuned on the dark stage; see tools/avatar/README) ───────
// Two-band toon: shadow = uShadowTint × key (linear), threshold on half-Lambert × AO.
export const SHADE = Object.freeze({
  // skin/face: soft peach-rose shadows that keep the lit hue (the official art's skin shadow
  // is a lighter, less saturated version of the lit tone); R1's 0.62/0.40/0.38 read brick-red
  // on the dark stage
  skin: { tint: [0.70, 0.62, 0.60], th: 0.50, soft: 0.03, rim: 0.4 },
  face: { tint: [0.80, 0.70, 0.70], th: 0.30, soft: 0.10, rim: 0.2 },
  hair: { tint: [0.48, 0.54, 0.70], th: 0.50, soft: 0.025, rim: 1.0 }, // silver, not slate (R1 had 0.40/0.46/0.62)
  cloth: { tint: [0.36, 0.33, 0.48], th: 0.50, soft: 0.025, rim: 1.0 },
});
// Inverted hull: width in CSS px, view-space push (m), colour = texture × tint.
export const OUTLINE = Object.freeze({
  hair: { px: 1.3, z: 0.004, tint: 0x4a5a78 },
  cloth: { px: 1.3, z: 0.002, tint: 0x3a3048 },
  skin: { px: 1.0, z: 0.002, tint: 0x7a5a58 }, // muted rose-brown (0x8a4a48 read as red lines)
  face: { px: 0.9, z: 0.003, tint: 0x7a5a58 },
});
export const LIGHT = Object.freeze({
  sky: 0xe6eaff, ground: 0x3b3550, hemi: 1.0, // fill
  key: 0xffffff, keyIntensity: 2.45, keyPos: [-1.4, 2.25, 1.8], // neutral (warm read peachy on her pale skin), front-left, high
  rimDir: [0.6, 0.5, -0.6], rimStrength: 0.75, keyRim: 0.55, // mood rim from back-right-top
});

// ── shader patches (identical code for every toon material; features via defines) ──
export const SWAY_VERT_PARS = `
#ifdef KL_SWAY
uniform float uTime; uniform vec2 uSway; uniform vec4 uSwayBox; uniform float uSwayAmp;
#endif`;
export const TOON_VERT_PARS = `
uniform vec3 uHeadCenter;
#ifdef KL_RING
varying vec3 vHeadDir;
#endif
${SWAY_VERT_PARS}`;
export const TOON_VERT_NORMAL = `
#ifdef KL_RING
vHeadDir = position - uHeadCenter;
#endif
#ifdef KL_FACE
objectNormal = normalize( mix( objectNormal, normalize( ( position - uHeadCenter ) * vec3( 1.0, 1.0, 0.8 ) ), 0.65 ) );
#endif`;
export const TOON_VERT_SWAY = `
#ifdef KL_SWAY
{
  float sw = 1.0 - smoothstep( uSwayBox.y, uSwayBox.x, position.y ); // 0 above the neck, 1 at the tips
  sw *= sw;
  float back = 1.0 - smoothstep( uSwayBox.w, uSwayBox.z, position.z ); // hair behind her back only
  transformed.x += sw * ( uSway.x + uSwayAmp * sin( uTime * 1.3 + position.y * 9.0 ) );
  transformed.z += sw * back * ( min( 0.0, uSway.y ) - uSwayAmp * ( 0.5 + 0.5 * sin( uTime * 0.9 + position.y * 7.0 ) ) );
}
#endif`;
export const TOON_FRAG_PARS = `
uniform vec3 uShadowTint; uniform float uTh; uniform float uSoft;
uniform vec3 uRimColor; uniform float uRimStrength; uniform vec3 uRimDir; uniform float uKeyRim; uniform float uRimMul;
uniform vec2 uKeySide;
#ifdef KL_RING
varying vec3 vHeadDir;
#endif
#ifdef KL_AO
uniform sampler2D uAoMap;
#endif
#ifdef KL_METAL
uniform sampler2D uMetalMap;
#endif
float gAo = 1.0;`;
export const TOON_LIGHTS = `
varying vec3 vViewPosition;
struct ToonMaterial { vec3 diffuseColor; };
void RE_Direct_Toon( const in IncidentLight directLight, const in vec3 geometryPosition, const in vec3 geometryNormal, const in vec3 geometryViewDir, const in vec3 geometryClearcoatNormal, const in ToonMaterial material, inout ReflectedLight reflectedLight ) {
  float nl = ( dot( geometryNormal, directLight.direction ) * 0.5 + 0.5 ) * gAo;
  float s = max( uSoft, fwidth( nl ) );
  float band = smoothstep( uTh - s, uTh + s, nl );
  reflectedLight.directDiffuse += mix( uShadowTint, vec3( 1.0 ), band ) * directLight.color * BRDF_Lambert( material.diffuseColor );
}
void RE_IndirectDiffuse_Toon( const in vec3 irradiance, const in vec3 geometryPosition, const in vec3 geometryNormal, const in vec3 geometryViewDir, const in vec3 geometryClearcoatNormal, const in ToonMaterial material, inout ReflectedLight reflectedLight ) {
  reflectedLight.indirectDiffuse += irradiance * mix( 1.0, gAo, 0.6 ) * BRDF_Lambert( material.diffuseColor );
}
#define RE_Direct RE_Direct_Toon
#define RE_IndirectDiffuse RE_IndirectDiffuse_Toon`;
export const TOON_FRAG_AO = `
#ifdef KL_AO
  gAo = mix( 1.0, texture2D( uAoMap, vMapUv ).r, 0.85 );
#endif`;
export const TOON_FRAG_FX = `
#if NUM_DIR_LIGHTS > 0
{
  vec3 Lv = directionalLights[ 0 ].direction;
  vec3 Lc = directionalLights[ 0 ].color;
  float facing = 1.0 - saturate( dot( normal, geometryViewDir ) );
  // key-side rim: stepped, only where the key comes from, tinted by albedo
  float side = saturate( dot( normalize( normal.xy + 1e-4 ), uKeySide ) * 1.5 - 0.2 );
  outgoingLight += smoothstep( 0.62, 0.67, facing ) * side * uKeyRim * uRimMul * diffuseColor.rgb * Lc * RECIPROCAL_PI;
  // mood rim from behind (back-right-top), stepped, coloured by setTint(); both rims fade toward
  // the face (uRimMul per role: GFL2 keeps material detail off the head)
  float rimM = smoothstep( 0.55, 0.61, facing ) * smoothstep( -0.05, 0.45, dot( normal, uRimDir ) );
  outgoingLight += rimM * uRimStrength * uRimMul * uRimColor * ( 0.35 + 0.65 * diffuseColor.rgb );
  #ifdef KL_RING
  {
    // angel ring: a latitude band on a sphere around the head (bind space, rides with the head),
    // sliding with camera elevation, made jagged by the strand texture
    float r = length( vHeadDir );
    float lat = vHeadDir.y / max( r, 1e-4 );
    float nearHead = 1.0 - smoothstep( 0.20, 0.27, r );
    vec3 upV = normalize( ( viewMatrix * vec4( 0.0, 1.0, 0.0, 0.0 ) ).xyz );
    float c = 0.32 - 0.25 * dot( geometryViewDir, upV );
    float x = lat + ( dot( diffuseColor.rgb, vec3( 0.333 ) ) - 0.7 ) * 0.45;
    float ring = smoothstep( c - 0.09, c - 0.05, x ) * ( 1.0 - smoothstep( c + 0.02, c + 0.06, x ) );
    ring *= nearHead * saturate( dot( normal, geometryViewDir ) * 2.5 ) * saturate( dot( normal, Lv ) * 2.0 + 0.6 );
    outgoingLight = outgoingLight * mix( 0.84, 1.0, ring ) + ring * vec3( 0.22, 0.24, 0.30 );
  }
  #endif
  #ifdef KL_METAL
  {
    // buckles and zips (metal mask): darker body, a hard stylised glint
    float mm = smoothstep( 0.45, 0.6, texture2D( uMetalMap, vMapUv ).b );
    vec3 H = normalize( Lv + geometryViewDir );
    float sp = smoothstep( 0.5, 0.58, pow( saturate( dot( normal, H ) ), 40.0 ) );
    outgoingLight = outgoingLight * ( 1.0 - 0.18 * mm ) + mm * sp * ( 0.3 + 0.7 * diffuseColor.rgb ) * Lc * 0.25;
  }
  #endif
}
#endif`;
export const OUTLINE_VERT_PARS = `
attribute vec4 _outline;
uniform vec2 uRes; uniform vec2 uWClamp; uniform float uDpr; uniform float uPx; uniform float uZ;
#ifdef KL_FACING
varying float vFacing;
#endif`;
export const OUTLINE_VERT_PROJECT = `
vec3 nV = normalize( normalMatrix * objectNormal ); // not transformedNormal: FLIP_SIDED negates it for BackSide
#ifdef KL_FACING
vFacing = dot( nV, normalize( -mvPosition.xyz ) );
#endif
mvPosition.xyz += normalize( mvPosition.xyz ) * uZ; // view-space push away from the camera (no polygonOffset)
gl_Position = projectionMatrix * mvPosition;
vec2 oDir = ( projectionMatrix * vec4( nV, 0.0 ) ).xy * uRes;
oDir = dot( oDir, oDir ) > 1e-10 ? normalize( oDir ) : vec2( 0.0 );
gl_Position.xy += oDir * ( 2.0 * uPx * uDpr * _outline.w / uRes ) * clamp( gl_Position.w, uWClamp.x, uWClamp.y );`;
// Skin and face are open meshes (skin under clothes is cut away in the build): through
// such a hole a BackSide hull shows the INSIDE of the far wall as a flat outline-
// coloured panel. Those faces look away from the camera; silhouette rims don't.
export const OUTLINE_FRAG_PARS = `
#ifdef KL_FACING
varying float vFacing;
#endif`;
export const OUTLINE_FRAG_CUT = `
#ifdef KL_FACING
if ( vFacing < -0.45 ) discard;
#endif`;
export const SHADOW_VERT = `
varying vec2 vXZ;
void main() { vec4 wp = modelMatrix * vec4( position, 1.0 ); vXZ = wp.xz; gl_Position = projectionMatrix * viewMatrix * wp; }`;
export const SHADOW_FRAG = `
uniform vec4 uHip; uniform vec4 uFeet; uniform vec4 uW; uniform vec3 uColor; uniform float uAlpha;
varying vec2 vXZ;
float blob( vec2 c, float r ) { vec2 d = ( vXZ - c ) / r; return exp( -2.0 * dot( d, d ) ); }
void main() {
  float a = uW.x * blob( uHip.xy, 0.28 ) + uW.y * blob( uHip.zw, 0.22 ) + uW.z * blob( uFeet.xy, 0.075 ) + uW.w * blob( uFeet.zw, 0.075 );
  gl_FragColor = vec4( uColor, min( a, 0.8 ) * uAlpha );
}`;
