#!/usr/bin/env python3
"""Exercise the real companion camera and UI using local, synthetic API data.

Requires the existing private assets/build/klukai.glb and Playwright Chromium.
No production API, inference, GPU service or provider credential is used.
Run: python3 tools/avatar/camera_e2e.py --out /tmp/klukai-camera-check
"""
import argparse
import functools
import http.server
import json
import math
from pathlib import Path
import socketserver
import threading
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
ANGLE = 2 * math.pi / 3


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def install_fixtures(page, glb):
    """Keep the full app/renderer real; replace only its network boundary.

    The wrapper enables the viewer's existing debug API and deterministic
    render options. It does not replace the camera or its input handling.
    """
    page.add_init_script("""localStorage.setItem('klukai_token', 'local-camera-test');
      localStorage.setItem('klukai_companion_prefs', JSON.stringify({
        mode:'model', appearance:'default', music:false, voice:false
      }));""")

    def api(route):
        path = urlparse(route.request.url).path
        if path == '/api/avatar/model':
            route.fulfill(body=glb, content_type='model/gltf-binary')
        elif path == '/api/avatar/models':
            route.fulfill(json={'models': [{'id': 'default', 'name': 'Klukai',
                'available': True, 'creator': 'Local test', 'license': 'Personal use'}]})
        elif path == '/api/her-day':
            route.fulfill(json={'status': {'label': 'The Elmo'}})
        elif path == '/api/portrait':
            route.fulfill(json={})
        elif path == '/api/affection':
            route.fulfill(json={'level': 1})
        else:
            route.fulfill(status=404, json={'error': 'No test fixture'})

    page.route('**/api/**', api)
    messages = []
    page.route_web_socket('**/ws?*', lambda ws: ws.on_message(lambda text: messages.append(json.loads(text))))

    def instrument(route):
        if 'camera-original=1' in route.request.url:
            route.continue_()
            return
        route.fulfill(content_type='text/javascript', body="""
          export * from './avatar3d.js?camera-original=1';
          import {createAvatar3D as create} from './avatar3d.js?camera-original=1';
          export async function createAvatar3D(container, options) {
            const avatar = await create(container, {...options, debug:true,
              life:false, reducedMotion:true, adaptiveResolution:false});
            window.cameraTestAvatar = avatar;
            return avatar;
          }
        """)

    page.route('**/companion/avatar3d.js*', instrument)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    return messages, errors


def view(page):
    return page.evaluate('cameraTestAvatar.debug.probe().orbit')


def settle(page):
    page.wait_for_timeout(450)


def settle_rotation(page):
    # OrbitControls damps per frame; software rendering can take much longer
    # than 450ms to finish a gesture. Observe convergence before testing hold.
    page.evaluate("""async () => {
      const deadline = performance.now() + 20000;
      let previous = cameraTestAvatar.debug.probe().orbit.dTheta;
      let stable = 0;
      while (performance.now() < deadline) {
        await new Promise(requestAnimationFrame);
        const current = cameraTestAvatar.debug.probe().orbit.dTheta;
        stable = Math.abs(current - previous) < 0.0003 ? stable + 1 : 0;
        if (stable >= 8) return;
        previous = current;
      }
      throw new Error('Camera rotation did not settle within 20 seconds');
    }""")


def reset(page):
    page.get_by_role('button', name='Reset view', exact=True).click()
    settle(page)
    state = view(page)
    assert abs(state['dTheta']) < 0.01 and abs(state['zoom'] - 1) < 0.01, state


def drag(page, start, end):
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(*end, steps=12)
    page.mouse.up()
    settle(page)


def pinch(cdp, start, end):
    def points(pair):
        return [{'x': x, 'y': y, 'id': i, 'radiusX': 3, 'radiusY': 3}
                for i, (x, y) in enumerate(pair)]
    cdp.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': points(start)})
    for step in range(1, 9):
        pair = [(a[0] + (b[0] - a[0]) * step / 8,
                 a[1] + (b[1] - a[1]) * step / 8) for a, b in zip(start, end)]
        cdp.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': points(pair)})
    cdp.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})


def run_viewport(browser, base, out, glb, mobile):
    width, height = (390, 844) if mobile else (1280, 800)
    context = browser.new_context(viewport={'width': width, 'height': height},
                                  has_touch=mobile, is_mobile=mobile, device_scale_factor=1)
    page = context.new_page()
    messages, errors = install_fixtures(page, glb)
    page.goto(base + '/flutter_app/web/companion.html')
    page.locator('#wake').click()
    page.wait_for_function('window.cameraTestAvatar?.ready', timeout=90000)
    page.get_by_role('button', name='Zoom in', exact=True).wait_for(timeout=3000)
    settle(page)
    reset(page)
    before = view(page)
    page.get_by_role('button', name='Zoom in', exact=True).click()
    settle(page)
    assert view(page)['zoom'] < before['zoom'] - 0.05
    page.get_by_role('button', name='Zoom out', exact=True).click()
    settle(page)
    assert abs(view(page)['zoom'] - 1) < 0.02

    page.locator('#input').fill('Keep my draft while resetting the camera.')
    page.get_by_role('button', name='Rotate right', exact=True).click()
    settle(page)
    assert abs(view(page)['dTheta']) > 0.1
    reset(page)
    assert page.locator('#input').input_value() == 'Keep my draft while resetting the camera.'

    if mobile:
        cdp = context.new_cdp_session(page)
        pinch(cdp, [(165, 320), (225, 320)], [(110, 320), (280, 320)])
        settle(page)
        close = view(page)['zoom']
        assert close < 0.95, view(page)
        pinch(cdp, [(110, 320), (280, 320)], [(165, 320), (225, 320)])
        settle(page)
        assert view(page)['zoom'] > close + 0.1, view(page)
        assert page.evaluate('visualViewport.scale') == 1
        assert page.evaluate('scrollY') == 0
        reset(page)
        pinch(cdp, [(300, 320)], [(90, 320)])
        settle(page)
        assert abs(view(page)['dTheta']) > 0.3
    else:
        page.mouse.move(640, 320)
        page.mouse.wheel(0, -500)
        settle(page)
        assert view(page)['zoom'] < 0.95
        page.mouse.wheel(0, 10000)
        settle(page)
        assert abs(view(page)['zoom'] - 1.8) < 0.02
        page.mouse.wheel(0, -10000)
        settle(page)
        assert abs(view(page)['zoom'] - 0.6) < 0.02
        reset(page)
        for _ in range(4):
            drag(page, (840, 320), (440, 320))
        positive = view(page)['dTheta']
        assert abs(positive - ANGLE) < 0.02, positive
        for _ in range(7):
            drag(page, (440, 320), (840, 320))
        negative = view(page)['dTheta']
        assert abs(negative + ANGLE) < 0.02, negative
        assert abs(positive - negative - 4 * math.pi / 3) < 0.04

    settle_rotation(page)
    chosen = view(page)
    assert abs(chosen['dTheta']) > 0.3, chosen
    page.wait_for_timeout(3100)
    assert abs(view(page)['dTheta'] - chosen['dTheta']) < 0.03, {'before': chosen, 'after': view(page)}
    assert abs(view(page)['zoom'] - chosen['zoom']) < 0.01
    page.screenshot(path=str(out / ('phone-rotated.png' if mobile else 'desktop-rotated.png')))
    page.evaluate('cameraTestAvatar.setInsets({top:75,bottom:200})')
    page.set_viewport_size({'width': width, 'height': height - 30})
    settle(page)
    assert abs(view(page)['dTheta'] - chosen['dTheta']) < 0.03
    assert abs(view(page)['zoom'] - chosen['zoom']) < 0.01
    page.get_by_role('button', name='Switch to portrait', exact=True).click()
    assert not page.get_by_role('button', name='Zoom in', exact=True).is_visible()
    assert page.locator('#portrait').is_visible()
    page.locator('#composer button[type=submit]').click()
    page.wait_for_timeout(100)
    assert any(m.get('content') == 'Keep my draft while resetting the camera.' for m in messages)
    page.get_by_role('button', name='Switch to 3D', exact=True).click()
    settle(page)
    assert abs(view(page)['dTheta'] - chosen['dTheta']) < 0.03
    assert abs(view(page)['zoom'] - chosen['zoom']) < 0.01
    reset(page)
    screenshot = out / ('phone.png' if mobile else 'desktop.png')
    page.screenshot(path=str(screenshot))
    assert not errors, errors
    result = {'viewport': f'{width}x{height}', 'status': 'passed', 'page_errors': errors,
              'screenshot': str(screenshot)}
    context.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path('/tmp/klukai-camera-check'))
    parser.add_argument('--viewport', choices=['all', 'desktop', 'phone'], default='all')
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
                    result = run_viewport(browser, f'http://127.0.0.1:{server.server_address[1]}',
                                          args.out, glb, mobile)
                    results.append(result)
                    print(json.dumps(result), flush=True)
            finally:
                browser.close()
                server.shutdown()
            (args.out / 'results.json').write_text(json.dumps(results, indent=2) + '\n')


if __name__ == '__main__':
    main()
