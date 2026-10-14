#!/usr/bin/env python3
"""Verify automatic idle life and the expanded reactions on the actual GLB.

Real browser input starts reactions. The existing debug clock then advances
the production mixer/scheduler, and measurements come from the rendered rig.
No provider, production API, or GPU service is used. Speech audio integration
and camera persistence remain covered by presence_e2e.py and camera_e2e.py.
"""
import argparse
import functools
import json
from pathlib import Path
import socketserver
import threading

from playwright.sync_api import sync_playwright

from camera_e2e import ROOT, Quiet, install_fixtures, reset
from presence_e2e import model_point, probe


def install_rig_probe(page):
    page.evaluate("""() => {
      window.sampleAvatarRig = () => {
        const {THREE, root} = cameraTestAvatar.debug;
        root.updateMatrixWorld(true);
        const bones = ['Head', 'LeftHand', 'RightHand', 'LeftForeArm', 'RightForeArm', 'Hips'];
        return bones.map(name => root.getObjectByName('mixamorig' + name)
          .getWorldPosition(new THREE.Vector3()).toArray());
      };
      window.rigTravel = samples => Math.max(...samples.flatMap(a => samples.flatMap(b =>
        a.map((p, i) => Math.hypot(...p.map((v, j) => v - b[i][j]))))));
    }""")


def advance(page, seconds):
    return page.evaluate('seconds => cameraTestAvatar.debug.run(seconds, {dt:1/30})', seconds)


def next_passive(page):
    """Let the normal scheduler choose a clip; never call debug.fidget()."""
    result = page.evaluate("""() => {
      const av = cameraTestAvatar, before = av.debug.probe().passive.count;
      for (let i = 0; i < 64; i++) {
        const p = av.debug.run(.5);
        if (p.passive.count > before && p.passive.active) {
          const samples = [sampleAvatarRig()], clip = p.clip;
          let current = p;
          for (let j = 0; j < 30 && current.passive.active; j++) {
            current = av.debug.run(.2);
            samples.push(sampleAvatarRig());
          }
          return {clip, passive:p.passive, travel:rigTravel(samples),
            restored:!current.passive.active, restoredClip:current.clip};
        }
      }
      throw new Error('No automatic passive animation within 32 idle seconds');
    }""")
    assert result['travel'] > 0.008, result
    assert result['restored'] and result['restoredClip'] == 'idle', result
    return result


def assert_suppressed(page, label):
    before = probe(page)['passive']['count']
    after = advance(page, 35)
    assert not after['passive']['active'], {'during': label, 'after': after}
    assert after['passive']['count'] == before, {'during': label, 'after': after}


def realtime_passive(page):
    """Observe a normal browser animation frame deadline, without debug.run."""
    initial = probe(page)['passive']['count']
    page.wait_for_function('(n) => cameraTestAvatar.debug.probe().passive.count > n',
        arg=initial, timeout=40000)
    started = probe(page)
    samples = [page.evaluate('sampleAvatarRig()')]
    for _ in range(6):
        page.wait_for_timeout(250)
        samples.append(page.evaluate('sampleAvatarRig()'))
    travel = page.evaluate('samples => rigTravel(samples)', samples)
    assert started['passive']['active'] and started['fidget'], started
    assert travel > .008, {'started': started, 'travel': travel}
    page.wait_for_function('!cameraTestAvatar.debug.probe().passive.active', timeout=10000)
    return {'clip': started['clip'], 'passive': started['passive'], 'travel': travel}


def run_viewport(browser, url, out, glb, mobile, *, realtime_only=False):
    width, height = (390, 844) if mobile else (1280, 800)
    label = 'phone' if mobile else 'desktop'
    context = browser.new_context(viewport={'width': width, 'height': height},
        has_touch=mobile, is_mobile=mobile, device_scale_factor=1)
    page = context.new_page()
    _, errors = install_fixtures(page, glb, presence=True)
    try:
        page.goto(url)
        page.locator('#wake').click()
        page.wait_for_function('window.cameraTestAvatar?.ready', timeout=90000)
        reset(page)
        install_rig_probe(page)
        realtime = realtime_passive(page) if not mobile or realtime_only else None
        if realtime_only:
            page.evaluate('cameraTestAvatar.dispose()')
            assert not errors, errors
            return {'viewport': f'{width}x{height}', 'status': 'passed',
                'realtime_passive': realtime, 'dispose': 'passed', 'page_errors': errors}
        reactions = []
        expected = {'salute', 'acknowledge', 'pleased', 'thoughtful', 'dismiss', 'bashful', 'playful'}
        for i in range(len(expected)):
            before = probe(page)['reactionCount']
            page.evaluate('cameraTestAvatar.setPaused(false)')
            if i % 2:
                page.get_by_role('button', name='Interact', exact=True).click()
            elif mobile:
                page.touchscreen.tap(*model_point(page))
            else:
                page.mouse.click(*model_point(page))
            page.wait_for_function('(n) => cameraTestAvatar.debug.probe().reactionCount > n', arg=before)
            kind = probe(page)['reaction']['kind']
            if kind in {'thoughtful', 'bashful', 'playful'}:
                advance(page, .8)
                page.screenshot(path=str(out / f'{label}-{kind}.png'))
            result = page.evaluate("""() => {
              const av = cameraTestAvatar, started = av.debug.probe(), samples = [sampleAvatarRig()];
              let p = started;
              for (let i = 0; i < 28 && p.reaction; i++) {
                p = av.debug.run(.45);
                samples.push(sampleAvatarRig());
              }
              return {kind:started.reaction.kind, clip:started.clip, travel:rigTravel(samples),
                restored:!p.reaction, restoredState:p.state, restoredClip:p.clip};
            }""")
            assert result['travel'] > 0.015, result
            assert result['restored'] and result['restoredState'] == 'idle', result
            reactions.append(result)
            # Cooldown uses actual input time; advancing animation must not
            # bypass its guard. Resume and allow the real input interval.
            page.evaluate('cameraTestAvatar.setPaused(false)')
            page.wait_for_timeout(2700)
        assert {r['kind'] for r in reactions} == expected, reactions

        passive = [next_passive(page) for _ in range(3)]
        assert all(a['clip'] != b['clip'] for a, b in zip(passive, passive[1:])), passive
        assert len({p['clip'] for p in passive}) >= 2, passive

        suppression = []
        for state in ['thinking', 'talking', 'sleep']:
            page.evaluate('state => cameraTestAvatar.setState(state)', state)
            assert_suppressed(page, state)
            suppression.append(state)
        page.evaluate("cameraTestAvatar.setState('idle'); cameraTestAvatar.setSpeaking(.8)")
        assert_suppressed(page, 'audio speaking')
        assert probe(page)['mouthOpen'] > .1
        page.evaluate('cameraTestAvatar.setSpeaking(0)')
        after_speech = next_passive(page)

        page.emulate_media(reduced_motion='reduce')
        page.wait_for_function('cameraTestAvatar.debug.probe().reducedMotion')
        assert_suppressed(page, 'reduced motion')
        page.emulate_media(reduced_motion='no-preference')
        page.wait_for_function('!cameraTestAvatar.debug.probe().reducedMotion')
        after_reduced = next_passive(page)

        # A real held pointer must inhibit passive gestures until released.
        page.evaluate('cameraTestAvatar.setPaused(false)')
        hit = model_point(page)
        page.mouse.move(*hit)
        page.mouse.down()
        assert_suppressed(page, 'held pointer')
        page.mouse.move(hit[0] + 25, hit[1] + 15, steps=3)
        page.mouse.up()
        after_pointer = next_passive(page)

        if mobile:
            page.evaluate('cameraTestAvatar.setPaused(false)')
            cdp = context.new_cdp_session(page)
            contacts = [{'x':155, 'y':300, 'id':0}, {'x':235, 'y':300, 'id':1}]
            cdp.send('Input.dispatchTouchEvent', {'type':'touchStart', 'touchPoints':contacts})
            assert_suppressed(page, 'pinch held')
            cdp.send('Input.dispatchTouchEvent', {'type':'touchEnd', 'touchPoints':contacts[1:]})
            assert_suppressed(page, 'pinch surviving finger')
            cdp.send('Input.dispatchTouchEvent', {'type':'touchEnd', 'touchPoints':[]})
            next_passive(page)
            suppression += ['pinch held', 'pinch surviving finger']

        # Wake after a parked view should receive a full quiet interval.
        page.evaluate('cameraTestAvatar.setPaused(true)')
        paused = probe(page)['passive']['count']
        page.wait_for_timeout(400)
        assert probe(page)['passive']['count'] == paused
        page.evaluate('cameraTestAvatar.setPaused(false)')
        resumed = advance(page, 3)
        assert not resumed['passive']['active'] and resumed['passive']['count'] == paused, resumed
        after_pause = next_passive(page)
        page.screenshot(path=str(out / f'{label}-idle.png'))
        page.evaluate('cameraTestAvatar.dispose()')
        assert not errors, errors
        return {'viewport': f'{width}x{height}', 'status': 'passed', 'reactions': reactions,
            'passive': passive, 'realtime_passive': realtime,
            'suppression': suppression + ['audio speaking', 'reduced motion', 'held pointer'],
            'recovered_after_speech': after_speech, 'recovered_after_reduced_motion': after_reduced,
            'recovered_after_pointer': after_pointer, 'recovered_after_pause': after_pause,
            'dispose': 'passed', 'page_errors': errors}
    except Exception:
        page.screenshot(path=str(out / f'{label}-failure.png'))
        (out / f'{label}-errors.json').write_text(json.dumps(errors, indent=2) + '\n')
        raise
    finally:
        context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path('/tmp/klukai-motion-check'))
    parser.add_argument('--viewport', choices=['all', 'desktop', 'phone'], default='all')
    parser.add_argument('--base-url', help='Companion HTML URL; API and WebSocket calls remain fixtures')
    parser.add_argument('--realtime-only', action='store_true', help='Run the wall-clock passive animation and disposal checks only')
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
                url = args.base_url or f'http://127.0.0.1:{server.server_address[1]}/flutter_app/web/companion.html'
                for mobile in ([False, True] if args.viewport == 'all' else [args.viewport == 'phone']):
                    result = run_viewport(browser, url, args.out, glb, mobile, realtime_only=args.realtime_only)
                    results.append(result)
                    print(json.dumps(result), flush=True)
            finally:
                browser.close()
                server.shutdown()
            (args.out / 'results.json').write_text(json.dumps(results, indent=2) + '\n')


if __name__ == '__main__':
    main()
