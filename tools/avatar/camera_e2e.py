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
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def install_fixtures(page, glb, *, presence=False, fixtures=None):
    """Keep the full app/renderer real; replace only its network boundary.

    The wrapper enables the viewer's existing debug API and deterministic
    render options. It does not replace the camera or its input handling.
    """
    # Seed once: re-seeding on every navigation would hide broken persistence.
    page.add_init_script("""localStorage.setItem('klukai_token', 'local-camera-test');
      if (!localStorage.getItem('klukai_companion_prefs'))
        localStorage.setItem('klukai_companion_prefs', JSON.stringify({
          mode:'model', appearance:'default', music:false, voice:false
        }));""")
    fixtures = fixtures if fixtures is not None else {}
    fixtures.setdefault('requests', [])
    fixtures.setdefault('sockets', [])

    def api(route):
        path = urlparse(route.request.url).path
        fixtures['requests'].append(path)
        if path == '/api/avatar/model':
            model = parse_qs(urlparse(route.request.url).query).get('model', ['default'])[0]
            body = (ROOT / 'assets/build/variants/klukai_garf.glb').read_bytes() if model == 'test-variant' else glb
            if model == 'test-variant' and fixtures.get('hold_variant'):
                fixtures.setdefault('held_models', []).append((route, body))
            else:
                route.fulfill(body=body, content_type='model/gltf-binary')
        elif path == '/api/avatar/models':
            route.fulfill(json={'models': [{'id': key, 'name': label,
                'available': True, 'creator': 'Local test', 'license': 'Personal use'}
                for key, label in [('default', 'Klukai'), ('test-variant', 'Klukai test appearance')]]})
        elif path == '/api/her-day':
            route.fulfill(json={'status': {'label': 'The Elmo'}})
        elif path == '/api/portrait':
            route.fulfill(json={})
        elif path == '/api/affection':
            route.fulfill(json={'level': 1})
        elif path == '/api/tts' and fixtures.get('audio'):
            route.fulfill(json={'audio': fixtures['audio'], 'format': 'wav'})
        else:
            route.fulfill(status=404, json={'error': 'No test fixture'})

    page.route('**/api/**', api)
    page.route('**/assets/assets/klukai_portrait.png', lambda route: route.fulfill(
        path=str(ROOT / 'flutter_app/assets/klukai_portrait.png'), content_type='image/png'))
    page.route('**/favicon.ico', lambda route: route.fulfill(status=204))
    messages = []
    def socket(ws):
        fixtures['sockets'].append(ws)
        ws.on_message(lambda text: messages.append(json.loads(text)))
    page.route_web_socket('**/ws?*', socket)

    def instrument(route):
        if 'camera-original=1' in route.request.url:
            route.continue_()
            return
        route.fulfill(content_type='text/javascript', body="""
          export * from './avatar3d.js?camera-original=1';
          import {createAvatar3D as create} from './avatar3d.js?camera-original=1';
          export async function createAvatar3D(container, options) {
            const avatar = await create(container, {...options, debug:true,
              life:PRESENCE_LIFE, reducedMotion:PRESENCE_REDUCED, adaptiveResolution:false});
            window.cameraTestAvatar = avatar;
            return avatar;
          }
        """.replace('PRESENCE_REDUCED', 'false' if presence else 'true')
           .replace('PRESENCE_LIFE', 'true' if presence else 'false'))

    page.route('**/companion/avatar3d.js*', instrument)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
    return messages, errors


def view(page):
    return page.evaluate('cameraTestAvatar.debug.probe().orbit')


def assert_view(page, expected):
    actual = view(page)
    for key in ('dTheta', 'dPhi', 'zoom', 'panY'):
        difference = angle_delta(actual[key], expected[key]) if key == 'dTheta' else actual[key] - expected[key]
        assert abs(difference) < 0.025, {'key': key, 'before': expected, 'after': actual}


def projected_framing(page):
    """Observe projection and pitch in the real scene, not camera bookkeeping."""
    return page.evaluate("""() => {
      const {THREE, root, camera, renderer, controls} = cameraTestAvatar.debug;
      root.updateMatrixWorld(true); camera.updateMatrixWorld();
      const centre = new THREE.Box3().setFromObject(root, true).getCenter(new THREE.Vector3());
      const p = centre.project(camera);
      const look = camera.getWorldDirection(new THREE.Vector3());
      const canvas = renderer.domElement.getBoundingClientRect();
      return {x:canvas.left + (p.x + 1) / 2 * canvas.width,
        y:canvas.top + (1 - p.y) / 2 * canvas.height, pitch:Math.asin(look.y)};
    }""")


def visible_geometry(page):
    """Project posed vertices and head through the renderer's actual camera."""
    return page.evaluate("""() => {
      const {THREE, root, camera, renderer} = cameraTestAvatar.debug;
      root.updateMatrixWorld(true); camera.updateMatrixWorld();
      const r = renderer.domElement.getBoundingClientRect();
      const top = document.querySelector('.topbar').getBoundingClientRect().bottom;
      const bottom = document.querySelector('.dock').getBoundingClientRect().top;
      const point = new THREE.Vector3();
      let sampled = 0, visible = 0, nearest = Infinity, farthest = -Infinity;
      let minY = Infinity, maxY = -Infinity;
      root.traverse(mesh => {
        if (!mesh.isMesh || !mesh.visible || !mesh.geometry?.attributes.position) return;
        const n = mesh.geometry.attributes.position.count;
        for (let i = 0; i < n; i += Math.max(1, Math.floor(n / 700))) {
          mesh.getVertexPosition(i, point).applyMatrix4(mesh.matrixWorld).project(camera);
          sampled++;
          nearest = Math.min(nearest, point.z); farthest = Math.max(farthest, point.z);
          const y = r.top + (1 - point.y) / 2 * r.height;
          if (Math.abs(point.x) < 1 && point.z > -1 && point.z < 1 && y > top && y < bottom) {
            visible++; minY = Math.min(minY, y); maxY = Math.max(maxY, y);
          }
        }
      });
      const head = root.getObjectByName('mixamorigHead').getWorldPosition(new THREE.Vector3()).project(camera);
      return {sampled, visible, nearest, farthest, visibleHeight:maxY-minY,
        near:camera.near, far:camera.far, top, bottom,
        head:{x:r.left+(head.x+1)/2*r.width, y:r.top+(1-head.y)/2*r.height, z:head.z}};
    }""")


def zoom_endpoint(page, *, close):
    expected = 0.2 if close else 3.0
    label = 'Zoom in' if close else 'Zoom out'
    for _ in range(22):
        page.get_by_role('button', name=label, exact=True).click()
        if abs(view(page)['zoom'] - expected) < 0.001:
            break
    settle(page)
    assert abs(view(page)['zoom'] - expected) < 0.001, view(page)
    page.get_by_role('button', name=label, exact=True).click()
    assert abs(view(page)['zoom'] - expected) < 0.001, view(page)


def close_up(page):
    """Use ordinary vertical dragging to bring her face into the close crop."""
    zoom_endpoint(page, close=True)
    before = visible_geometry(page)
    target_y = before['top'] + (before['bottom'] - before['top']) * 0.48
    start_y = (before['top'] + before['bottom']) / 2
    width = page.viewport_size['width']
    for _ in range(12):
        delta = max(-180, min(180, target_y - visible_geometry(page)['head']['y']))
        if abs(delta) < 12:
            break
        drag(page, (width * 0.6, start_y), (width * 0.6, start_y + delta))
    after = visible_geometry(page)
    assert after['visible'] > 100, after
    assert after['top'] < after['head']['y'] < after['bottom'], after
    assert -1 < after['head']['z'] < 1, after
    assert abs(view(page)['panY']) > 0.05, view(page)
    return after


def angle_delta(after, before):
    return (after - before + math.pi) % (2 * math.pi) - math.pi


def check_full_body(page):
    bounds = page.evaluate("""() => {
      const {THREE, root, camera, renderer} = cameraTestAvatar.debug;
      root.updateMatrixWorld(true);
      camera.updateMatrixWorld();
      const box = new THREE.Box3().setFromObject(root, true);
      const centre = box.getCenter(new THREE.Vector3());
      const canvas = renderer.domElement.getBoundingClientRect();
      const screenY = y => {
        const point = new THREE.Vector3(centre.x, y, centre.z).project(camera);
        return canvas.top + (1 - point.y) / 2 * canvas.height;
      };
      return {head:screenY(box.max.y), feet:screenY(box.min.y),
        top:document.querySelector('.topbar').getBoundingClientRect().bottom,
        bottom:document.querySelector('.dock').getBoundingClientRect().top};
    }""")
    assert bounds['head'] > bounds['top'], bounds
    assert bounds['feet'] < bounds['bottom'], bounds


def check_dock_layout(page):
    """Measure the visible menu, including narrow and short touch viewports."""
    layout = page.evaluate("""() => {
      const rect = el => {
        const r = el.getBoundingClientRect();
        return {left:r.left, right:r.right, top:r.top, bottom:r.bottom, height:r.height};
      };
      const dock = document.querySelector('.dock');
      return {width:innerWidth, height:innerHeight, dock:rect(dock),
        topbar:rect(document.querySelector('.topbar')),
        paddingTop:parseFloat(getComputedStyle(dock).paddingTop),
        subtitle:rect(document.querySelector('#subtitle')),
        controls:rect(document.querySelector('#camera-controls')),
        composer:rect(document.querySelector('#composer')),
        buttons:[...document.querySelectorAll('#camera-controls button')].map(button => {
          const r = rect(button), hit = document.elementFromPoint((r.left+r.right)/2, (r.top+r.bottom)/2);
          return {...r, label:button.getAttribute('aria-label') || button.textContent,
            reachable:hit === button || button.contains(hit)};
        })};
    }""")
    assert layout['subtitle']['bottom'] <= layout['controls']['top'], layout
    assert layout['controls']['bottom'] < layout['composer']['top'], layout
    assert layout['composer']['bottom'] < layout['height'], layout
    assert abs(layout['dock']['bottom'] - layout['height']) < 1, layout
    assert layout['dock']['top'] > layout['topbar']['bottom'], layout
    # The former first item began at dock top + padding. The empty subtitle
    # now gives the menu 50px more downward room at every viewport size.
    layout['menu_lower_by_pixels'] = (layout['controls']['top'] -
        layout['dock']['top'] - layout['paddingTop'])
    assert layout['menu_lower_by_pixels'] >= 48, layout
    for button in layout['buttons']:
        assert 0 <= button['left'] < button['right'] <= layout['width'], button
        assert layout['subtitle']['bottom'] <= button['top'] < button['bottom'] <= layout['composer']['top'], button
        assert button['reachable'], button
    return layout


def run_layout_viewports(browser, base, out, glb):
    results = []
    for width, height in [(1280, 800), (390, 844), (320, 740), (844, 390), (568, 320)]:
        context = browser.new_context(viewport={'width': width, 'height': height},
            has_touch=True, is_mobile=width < 500, device_scale_factor=1)
        page = context.new_page()
        _, errors = install_fixtures(page, glb)
        try:
            page.goto(base)
            page.locator('#wake').click()
            page.wait_for_function('window.cameraTestAvatar?.ready', timeout=90000)
            settle(page)
            layout = check_dock_layout(page)
            check_full_body(page)
            page.screenshot(path=str(out / f'layout-{width}x{height}-empty.png'))
            # A visible reply must stay above the controls and keep all targets
            # usable, including the compact landscape rule.
            page.locator('#subtitle').evaluate("el => el.textContent = 'Commander, I am ready when you are.'")
            reply_layout = check_dock_layout(page)
            page.locator('#subtitle').evaluate("el => el.textContent = 'Commander, I am ready when you are. '.repeat(15)")
            page.wait_for_timeout(1000)
            long_reply_layout = check_dock_layout(page)
            check_full_body(page)
            screenshot = out / f'layout-{width}x{height}.png'
            page.screenshot(path=str(screenshot))
            assert not errors, errors
            results.append({'viewport': f'{width}x{height}', 'status': 'passed',
                'layout': layout, 'reply_layout': reply_layout, 'long_reply_layout': long_reply_layout,
                'screenshot': str(screenshot)})
        finally:
            context.close()
    return results


def run_zoom_landscape(browser, base, out, glb):
    context = browser.new_context(viewport={'width': 568, 'height': 320}, has_touch=True)
    page = context.new_page()
    _, errors = install_fixtures(page, glb)
    try:
        page.goto(base)
        page.locator('#wake').click()
        page.wait_for_function('window.cameraTestAvatar?.ready', timeout=90000)
        settle(page)
        zoom_endpoint(page, close=False)
        wide = visible_geometry(page)
        assert wide['far'] > 30, wide
        assert wide['visible'] > 100 and wide['visibleHeight'] > 10, wide
        assert -1 < wide['nearest'] <= wide['farthest'] < 1, wide
        page.screenshot(path=str(out / 'landscape-wide.png'))
        saved = view(page)
        page.reload()
        page.locator('#wake').click()
        page.wait_for_function('window.cameraTestAvatar?.ready', timeout=90000)
        settle(page)
        assert_view(page, saved)
        assert visible_geometry(page)['far'] > 30
        reset(page)
        check_full_body(page)
        assert not errors, errors
        return {'viewport': '568x320', 'status': 'passed', 'wide': wide, 'restored_view': saved,
            'screenshot': str(out / 'landscape-wide.png')}
    finally:
        context.close()


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
    assert abs(state['panY']) < 0.01, state


def drag(page, start, end):
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(*end, steps=12)
    page.mouse.up()
    settle(page)


def pinch(cdp, start, end, *, staggered=False, replace_contact=False):
    def points(pair):
        return [{'x': x, 'y': y, 'id': i, 'radiusX': 3, 'radiusY': 3}
                for i, (x, y) in enumerate(pair)]
    cdp.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': points(start)})
    for step in range(1, 9):
        pair = [(a[0] + (b[0] - a[0]) * step / 8,
                 a[1] + (b[1] - a[1]) * step / 8) for a, b in zip(start, end)]
        cdp.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': points(pair)})
    if staggered:
        assert len(end) == 2
        # Native touch streams often lift one finger before the other. The
        # survivor must not become an accidental drag or character tap.
        # CDP touchEnd lists the ending contacts (DOM TouchEvent.touches lists
        # survivors). Lift id1, then keep moving id0 without creating a new tap.
        ending = points(end)[:1] if replace_contact else points(end)[1:]
        cdp.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': ending})
        survivor = [(end[0][0] + 25, end[0][1] + 20)]
        # With replace_contact, id0 was lifted: this move starts a new contact
        # while id1 is still held. The entire sequence must remain a pinch.
        cdp.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': points(survivor)})
    cdp.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})


def run_viewport(browser, base, out, glb, mobile):
    width, height = (390, 844) if mobile else (1280, 800)
    context = browser.new_context(viewport={'width': width, 'height': height},
                                  has_touch=mobile, is_mobile=mobile, device_scale_factor=1)
    page = context.new_page()
    fixtures = {}
    messages, errors = install_fixtures(page, glb, fixtures=fixtures)
    page.goto(base)
    page.locator('#wake').click()
    page.wait_for_function('window.cameraTestAvatar?.ready', timeout=90000)
    page.get_by_role('button', name='Zoom in', exact=True).wait_for(timeout=3000)
    settle(page)
    reset(page)
    check_full_body(page)
    dock_layout = check_dock_layout(page)
    before = view(page)
    page.get_by_role('button', name='Zoom in', exact=True).click()
    settle(page)
    assert view(page)['zoom'] < before['zoom'] - 0.05
    page.get_by_role('button', name='Zoom out', exact=True).click()
    settle(page)
    assert abs(view(page)['zoom'] - 1) < 0.02
    zoom_endpoint(page, close=False)
    wide = visible_geometry(page)
    assert wide['visible'] > 100 and wide['visibleHeight'] > 10, wide
    assert -1 < wide['nearest'] <= wide['farthest'] < 1, wide
    page.screenshot(path=str(out / ('phone-wide.png' if mobile else 'desktop-wide.png')))
    reset(page)
    close_crop = close_up(page)
    page.screenshot(path=str(out / ('phone-close.png' if mobile else 'desktop-close.png')))
    saved_close = view(page)
    page.reload()
    page.locator('#wake').click()
    page.wait_for_function('window.cameraTestAvatar?.ready', timeout=90000)
    settle(page)
    assert_view(page, saved_close)
    reset(page)

    page.locator('#input').fill('Keep my draft while resetting the camera.')
    page.get_by_role('button', name='Rotate right', exact=True).click()
    settle(page)
    assert abs(view(page)['dTheta']) > 0.1
    reset(page)
    assert page.locator('#input').input_value() == 'Keep my draft while resetting the camera.'
    before_position = projected_framing(page)
    page.get_by_role('button', name='Move Klukai up', exact=True).click()
    settle(page)
    assert projected_framing(page)['y'] < before_position['y'] - 5
    page.get_by_role('button', name='Move Klukai down', exact=True).click()
    settle(page)
    assert abs(projected_framing(page)['y'] - before_position['y']) < 5
    reset(page)

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
        for _ in range(3):
            pinch(cdp, [(165, 320), (225, 320)], [(70, 320), (320, 320)])
        settle(page)
        assert abs(view(page)['zoom'] - 0.2) < 0.02, view(page)
        for _ in range(4):
            pinch(cdp, [(70, 320), (320, 320)], [(165, 320), (225, 320)])
        settle(page)
        assert abs(view(page)['zoom'] - 3) < 0.02, view(page)
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
        assert abs(view(page)['zoom'] - 3) < 0.02
        page.mouse.wheel(0, -10000)
        settle(page)
        assert abs(view(page)['zoom'] - 0.2) < 0.02
        reset(page)
        drag(page, (840, 320), (440, 320))

    # Buttons must pass the old ±120° stops, cross the back seam and keep
    # turning in both directions. Sum wrapped deltas to observe full turns.
    reset(page)
    for label, sign in [('Rotate right', 1), ('Rotate left', -1)]:
        previous = view(page)['dTheta']
        travelled = 0
        for _ in range(25):
            page.get_by_role('button', name=label, exact=True).click()
            current = view(page)['dTheta']
            delta = angle_delta(current, previous)
            assert abs(delta - sign * math.pi / 12) < 0.02, delta
            travelled += delta
            previous = current
        assert abs(travelled) > 2 * math.pi, travelled
    reset(page)
    if mobile:
        pinch(cdp, [(300, 320)], [(90, 320)])
    else:
        drag(page, (840, 320), (440, 320))

    settle_rotation(page)
    before_position = projected_framing(page)
    before_vertical = view(page)
    if mobile:
        pinch(cdp, [(195, 270)], [(195, 360)])
    else:
        drag(page, (640, 270), (640, 360))
    settle_rotation(page)
    after_position = projected_framing(page)
    assert after_position['y'] > before_position['y'] + 25, {'before': before_position, 'after': after_position}
    assert abs(after_position['pitch'] - before_position['pitch']) < 0.015, {'before': before_position, 'after': after_position}
    assert abs(view(page)['dPhi'] - before_vertical['dPhi']) < 0.015
    assert abs(view(page)['panY']) > 0.05
    # Persist a distance beyond the former close limit across the existing
    # resize, mode, pending appearance replacement and reload checks below.
    zoom_endpoint(page, close=True)
    chosen = view(page)
    assert abs(chosen['dTheta']) > 0.3, chosen
    page.wait_for_timeout(3100)
    assert_view(page, chosen)
    page.screenshot(path=str(out / ('phone-rotated.png' if mobile else 'desktop-rotated.png')))
    page.evaluate('cameraTestAvatar.setInsets({top:75,bottom:200})')
    page.set_viewport_size({'width': width, 'height': height - 30})
    settle(page)
    assert_view(page, chosen)
    page.get_by_role('button', name='Switch to portrait', exact=True).click()
    assert not page.get_by_role('button', name='Zoom in', exact=True).is_visible()
    assert page.locator('#portrait').is_visible()
    page.locator('#composer button[type=submit]').click()
    page.wait_for_timeout(100)
    assert any(m.get('content') == 'Keep my draft while resetting the camera.' for m in messages)
    page.get_by_role('button', name='Switch to 3D', exact=True).click()
    settle(page)
    assert_view(page, chosen)
    fixtures['hold_variant'] = True
    page.get_by_role('button', name='Choose 3D appearance', exact=True).click()
    page.locator('#appearance-select').select_option('test-variant')
    for _ in range(50):
        if fixtures.get('held_models'):
            break
        page.wait_for_timeout(20)
    assert fixtures.get('held_models'), 'Expected the pending appearance request'
    page.get_by_role('button', name='Move Klukai up', exact=True).click()
    page.get_by_role('button', name='Rotate left', exact=True).click()
    settle(page)
    chosen = view(page)
    fixtures['hold_variant'] = False
    for route, body in fixtures.pop('held_models'):
        route.fulfill(body=body, content_type='model/gltf-binary')
    page.wait_for_function("JSON.parse(localStorage.getItem('klukai_companion_prefs')).appearance === 'test-variant'")
    settle(page)
    assert_view(page, chosen)
    page.reload()
    page.locator('#wake').click()
    page.wait_for_function('window.cameraTestAvatar?.ready', timeout=90000)
    settle(page)
    assert_view(page, chosen)
    assert page.locator('#appearance-select').input_value() == 'test-variant'
    reset(page)
    check_full_body(page)
    screenshot = out / ('phone.png' if mobile else 'desktop.png')
    page.screenshot(path=str(screenshot))
    page.reload()
    page.locator('#wake').click()
    page.wait_for_function('window.cameraTestAvatar?.ready', timeout=90000)
    settle(page)
    reset_state = view(page)
    assert abs(reset_state['panY']) < 0.01 and abs(reset_state['dTheta']) < 0.01, reset_state
    assert abs(reset_state['zoom'] - 1) < 0.01, reset_state
    assert not errors, errors
    result = {'viewport': f'{width}x{height}', 'status': 'passed', 'page_errors': errors,
              'vertical_drag_pixels': after_position['y'] - before_position['y'],
              'vertical_drag_pitch_change': after_position['pitch'] - before_position['pitch'],
              'menu_lower_by_pixels': dock_layout['menu_lower_by_pixels'],
              'zoom_extremes': {'close': close_crop, 'wide': wide},
              'restored_view': chosen,
              'screenshot': str(screenshot)}
    context.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path('/tmp/klukai-camera-check'))
    parser.add_argument('--viewport', choices=['all', 'desktop', 'phone'], default='all')
    parser.add_argument('--base-url', help='Companion HTML URL; all API and WebSocket calls remain local fixtures')
    parser.add_argument('--layout-only', action='store_true', help='Check menu geometry at desktop and touch viewport sizes')
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
                base = args.base_url or f'http://127.0.0.1:{server.server_address[1]}/flutter_app/web/companion.html'
                if args.layout_only:
                    results = run_layout_viewports(browser, base, args.out, glb)
                    print(json.dumps(results), flush=True)
                else:
                    for mobile in ([False, True] if args.viewport == 'all' else [args.viewport == 'phone']):
                        result = run_viewport(browser, base, args.out, glb, mobile)
                        results.append(result)
                        print(json.dumps(result), flush=True)
                    if args.viewport == 'all':
                        result = run_zoom_landscape(browser, base, args.out, glb)
                        results.append(result)
                        print(json.dumps(result), flush=True)
            finally:
                browser.close()
                server.shutdown()
            (args.out / 'results.json').write_text(json.dumps(results, indent=2) + '\n')


if __name__ == '__main__':
    main()
