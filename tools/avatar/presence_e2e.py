#!/usr/bin/env python3
"""Check avatar interaction and speech through the complete companion app.

Uses the private GLB, real WebGL renderer, real input events, real Web Audio
decoding/playback/metering, and local network fixtures only. No GPU/provider
requests are made. Camera persistence/framing checks live in camera_e2e.py.
Run: python3 tools/avatar/presence_e2e.py --out /tmp/klukai-presence-check
"""
import argparse
import base64
import functools
import http.server
import io
import json
import math
from pathlib import Path
import socketserver
import struct
import threading
import wave

from playwright.sync_api import sync_playwright

from camera_e2e import ROOT, Quiet, drag, install_fixtures, pinch, reset, settle, view


def speech_fixture():
    """An audible, varying envelope; production voice code must meter it."""
    buffer = io.BytesIO()
    rate, seconds = 24000, 15
    with wave.open(buffer, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b''.join(struct.pack('<h', int(11000 *
            (0.65 + 0.35 * math.sin(2 * math.pi * 2.3 * i / rate)) *
            math.sin(2 * math.pi * 220 * i / rate))) for i in range(rate * seconds)))
    return base64.b64encode(buffer.getvalue()).decode()


def probe(page):
    return page.evaluate('cameraTestAvatar.debug.probe()')


def model_point(page, hit=True):
    """Find a visible target on the actual model, or empty canvas beside it."""
    return page.evaluate("""hit => {
      const {THREE, root, camera, renderer} = cameraTestAvatar.debug;
      root.updateMatrixWorld(true); camera.updateMatrixWorld();
      const canvas = renderer.domElement, r = canvas.getBoundingClientRect();
      const ray = new THREE.Raycaster();
      const h = root.getObjectByName('mixamorigHead');
      const head = h.getWorldPosition(new THREE.Vector3()).project(camera);
      const cx = r.left + (head.x + 1) / 2 * r.width;
      const cy = r.top + (1 - head.y) / 2 * r.height;
      const candidates = hit ? [[cx, cy], [cx, cy + 20], [cx, cy + 55]] :
        [[r.left + 20, r.top + r.height * .4], [r.right - 20, r.top + r.height * .4]];
      for (const [x, y] of candidates) {
        if (document.elementFromPoint(x, y) !== canvas) continue;
        ray.setFromCamera(new THREE.Vector2((x-r.left)/r.width*2-1, 1-(y-r.top)/r.height*2), camera);
        const intersects = ray.intersectObject(root, true).some(i => i.object.isMesh && i.object.visible);
        if (intersects === hit) return [x, y];
      }
      throw new Error('Could not locate the requested visible model/canvas target');
    }""", hit)


def rendered_speech_sample(page):
    """Read actual scene geometry and skeleton, independently of probe values.

    Compare the rendered mouth vertex with the same posed vertex at zero
    speech weight. Both use THREE's getVertexPosition (morph + skin), so a
    nonzero debug mouth flag without a real deformation cannot pass.
    """
    return page.evaluate("""() => {
      const {THREE, root, camera, renderer} = cameraTestAvatar.debug;
      root.updateMatrixWorld(true); camera.updateMatrixWorld();
      let mouth = null;
      root.traverse(mesh => {
        if (!mesh.isSkinnedMesh || mouth) return;
        const entry = Object.entries(mesh.morphTargetDictionary || {}).find(([name]) => /speechMouth|mouthOpen|jawOpen|viseme_aa/i.test(name));
        if (!entry) return;
        const [name, index] = entry, target = mesh.geometry.morphAttributes.position[index];
        const base = mesh.geometry.attributes.position;
        let vertex = -1, distance = 0;
        for (let i = 0; i < target.count; i++) {
          const p = new THREE.Vector3().fromBufferAttribute(target, i);
          if (!mesh.geometry.morphTargetsRelative) p.sub(new THREE.Vector3().fromBufferAttribute(base, i));
          if (p.lengthSq() > distance) { vertex = i; distance = p.lengthSq(); }
        }
        const weight = mesh.morphTargetInfluences[index];
        const open = mesh.getVertexPosition(vertex, new THREE.Vector3()).applyMatrix4(mesh.matrixWorld);
        mesh.morphTargetInfluences[index] = 0;
        const closed = mesh.getVertexPosition(vertex, new THREE.Vector3()).applyMatrix4(mesh.matrixWorld);
        mesh.morphTargetInfluences[index] = weight;
        const a = open.clone().project(camera), b = closed.clone().project(camera);
        const r = renderer.domElement.getBoundingClientRect();
        mouth = {name, weight, authoredDelta:Math.sqrt(distance),
          renderedDelta:open.distanceTo(closed),
          pixels:Math.hypot((a.x-b.x)*r.width/2, (a.y-b.y)*r.height/2)};
      });
      const hands = ['mixamorigLeftHand', 'mixamorigRightHand'].map(name => {
        const bone = root.getObjectByName(name), p = bone.getWorldPosition(new THREE.Vector3());
        const n = p.clone().project(camera), r = renderer.domElement.getBoundingClientRect();
        return {name, xyz:p.toArray(), screen:[r.left+(n.x+1)*r.width/2, r.top+(1-n.y)*r.height/2]};
      });
      const p = cameraTestAvatar.debug.probe();
      return {mouth, hands, mouthOpen:p.mouthOpen, talkWeight:p.talkWeight,
        visibleBand:[document.querySelector('.topbar').getBoundingClientRect().bottom,
          document.querySelector('.dock').getBoundingClientRect().top],
        speaking:document.querySelector('#stage').classList.contains('speaking')};
    }""")


def run_viewport(browser, url, out, glb, mobile, *, gestures_only=False):
    width, height = (390, 844) if mobile else (1280, 800)
    label = 'phone' if mobile else 'desktop'
    context = browser.new_context(viewport={'width': width, 'height': height},
        has_touch=mobile, is_mobile=mobile, device_scale_factor=1)
    page = context.new_page()
    fixtures = {'audio': speech_fixture()}
    messages, errors = install_fixtures(page, glb, presence=True, fixtures=fixtures)
    try:
        page.goto(url)
        page.locator('#wake').click()
        page.wait_for_function('window.cameraTestAvatar?.ready', timeout=90000)
        reset(page)
        initial = probe(page)['reactionCount']
        background = model_point(page, False)
        page.mouse.click(*background)
        settle(page)
        assert probe(page)['reactionCount'] == initial, 'Background click triggered a reaction'
        hit = model_point(page)
        if mobile:
            cdp = context.new_cdp_session(page)
            pinch(cdp, [hit], [(hit[0] + 60, hit[1] + 40)])
            settle(page)
            assert probe(page)['reactionCount'] == initial, 'Touch drag triggered a reaction'
            reset(page)
            pinch(cdp, [(155, 300), (235, 300)], [(120, 300), (270, 300)])
            settle(page)
            assert probe(page)['reactionCount'] == initial, 'Pinch triggered a reaction'
            pinch_view = view(page)
            pinch(cdp, [(155, 300), (235, 300)], [(120, 300), (270, 300)], staggered=True)
            settle(page)
            assert probe(page)['reactionCount'] == initial, 'Staggered pinch release triggered a reaction'
            assert abs(view(page)['panY'] - pinch_view['panY']) < 0.01
            assert abs(view(page)['dTheta'] - pinch_view['dTheta']) < 0.01
            pinch(cdp, [(155, 300), (235, 300)], [(120, 300), (270, 300)],
                staggered=True, replace_contact=True)
            settle(page)
            assert probe(page)['reactionCount'] == initial, 'Replacement finger during pinch triggered a reaction'
            assert abs(view(page)['panY'] - pinch_view['panY']) < 0.01
            assert abs(view(page)['dTheta'] - pinch_view['dTheta']) < 0.01
        else:
            drag(page, hit, (hit[0] + 80, hit[1] + 50))
            assert probe(page)['reactionCount'] == initial, 'Mouse drag triggered a reaction'
        reset(page)
        if gestures_only:
            assert not errors, errors
            page.screenshot(path=str(out / f'{label}-gestures.png'))
            return {'viewport': f'{width}x{height}', 'status': 'passed',
                'checks': 'background, drag, pinch, staggered lift, replacement finger', 'page_errors': errors}

        reactions = []
        for i in range(3):
            hit = model_point(page)
            if mobile:
                page.touchscreen.tap(*hit)
            else:
                page.mouse.click(*hit)
            page.wait_for_function('(n) => cameraTestAvatar.debug.probe().reactionCount > n', arg=initial + i)
            state = probe(page)
            assert state['reaction'], state
            reactions.append(state['reaction'])
            if i == 0:
                page.wait_for_timeout(250)
                page.screenshot(path=str(out / f'{label}-reaction.png'))
            page.wait_for_function('!cameraTestAvatar.debug.probe().reaction', timeout=15000)
            page.wait_for_timeout(300)
        assert len({r['kind'] for r in reactions}) >= 2, reactions
        page.get_by_role('button', name='Interact', exact=True).click()
        page.wait_for_function('(n) => cameraTestAvatar.debug.probe().reactionCount > n', arg=initial + 3)
        page.wait_for_function('!cameraTestAvatar.debug.probe().reaction', timeout=15000)
        adopted = page.evaluate("""() => {
          const av = cameraTestAvatar;
          if (!av.react('salute')) throw new Error('Could not begin salute adoption check');
          av.setState('greet');
          const result = av.debug.run(4);
          av.setPaused(false);
          return result;
        }""")
        assert adopted['state'] == 'idle' and not adopted['reaction'], adopted

        # Real application WebSocket → TTS request → WAV decode → analyser → avatar.
        page.get_by_role('button', name='Voice', exact=True).click()
        assert page.get_by_role('button', name='Voice', exact=True).get_attribute('aria-pressed') == 'true'
        fixtures['sockets'][-1].send(json.dumps({'type': 'done', 'text': 'Commander, ready.'}))
        page.wait_for_function('document.querySelector("#stage").classList.contains("speaking")', timeout=15000)
        samples = []
        for i in range(12):
            page.wait_for_timeout(180)
            samples.append(rendered_speech_sample(page))
            if i == 3:
                page.screenshot(path=str(out / f'{label}-speaking.png'))
        assert '/api/tts' in fixtures['requests'], fixtures['requests']
        assert any(s['speaking'] and s['talkWeight'] > 0.15 for s in samples), samples
        assert any(s['mouth'] and s['mouth']['renderedDelta'] > 0.0005 for s in samples), samples
        assert max(s['mouth']['weight'] for s in samples) - min(s['mouth']['weight'] for s in samples) > 0.03, samples
        hand_motion = max(math.dist(a['hands'][h]['xyz'], b['hands'][h]['xyz'])
            for a in samples for b in samples for h in (0, 1))
        assert hand_motion > 0.015, {'hand_motion_metres': hand_motion, 'samples': samples}
        assert all(any(s['visibleBand'][0] < s['hands'][h]['screen'][1] < s['visibleBand'][1]
            and 0 < s['hands'][h]['screen'][0] < width for s in samples) for h in (0, 1)), samples
        page.wait_for_function('!document.querySelector("#stage").classList.contains("speaking")', timeout=25000)
        page.wait_for_function('cameraTestAvatar.debug.probe().mouthOpen < 0.01 && cameraTestAvatar.debug.probe().talkWeight < 0.01', timeout=10000)
        silence = rendered_speech_sample(page)
        assert silence['mouth']['renderedDelta'] < 0.0001, silence
        page.screenshot(path=str(out / f'{label}-silent.png'))
        page.get_by_role('button', name='Choose 3D appearance', exact=True).click()
        page.locator('#appearance-select').select_option('test-variant')
        page.wait_for_function("JSON.parse(localStorage.getItem('klukai_companion_prefs')).appearance === 'test-variant'")
        page.evaluate('cameraTestAvatar.debug.run(0.4, {speaking:0.8})')
        variant = rendered_speech_sample(page)
        assert variant['mouth'] and variant['mouth']['renderedDelta'] > 0.0005, variant
        page.screenshot(path=str(out / f'{label}-variant-speaking.png'))
        page.evaluate('cameraTestAvatar.debug.run(1.2, {speaking:0})')
        assert rendered_speech_sample(page)['mouth']['renderedDelta'] < 0.0001
        assert not errors, errors
        result = {'viewport': f'{width}x{height}', 'status': 'passed', 'reactions': reactions,
            'max_mouth_deformation_metres': max(s['mouth']['renderedDelta'] for s in samples),
            'max_mouth_deformation_pixels': max(s['mouth']['pixels'] for s in samples),
            'hand_motion_metres': hand_motion, 'silent': silence, 'variant_mouth': variant['mouth'], 'page_errors': errors}
        (out / f'{label}-samples.json').write_text(json.dumps(samples, indent=2) + '\n')
        return result
    except Exception:
        page.screenshot(path=str(out / f'{label}-failure.png'))
        (out / f'{label}-errors.json').write_text(json.dumps(errors, indent=2) + '\n')
        raise
    finally:
        context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path('/tmp/klukai-presence-check'))
    parser.add_argument('--viewport', choices=['all', 'desktop', 'phone'], default='all')
    parser.add_argument('--base-url', help='Companion HTML URL; all API and WebSocket calls remain fixtures')
    parser.add_argument('--gestures-only', action='store_true', help='Run only the input lifecycle regressions')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    glb = (ROOT / 'assets/build/klukai.glb').read_bytes()
    handler = functools.partial(Quiet, directory=str(ROOT))
    with socketserver.ThreadingTCPServer(('127.0.0.1', 0), handler) as server:
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=['--no-sandbox', '--use-gl=angle',
                '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'])
            results = []
            try:
                for mobile in ([False, True] if args.viewport == 'all' else [args.viewport == 'phone']):
                    result = run_viewport(browser, args.base_url or
                        f'http://127.0.0.1:{server.server_address[1]}/flutter_app/web/companion.html',
                        args.out, glb, mobile, gestures_only=args.gestures_only)
                    results.append(result)
                    print(json.dumps(result), flush=True)
            finally:
                browser.close()
                server.shutdown()
            (args.out / 'results.json').write_text(json.dumps(results, indent=2) + '\n')


if __name__ == '__main__':
    main()
