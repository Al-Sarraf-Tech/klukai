#!/usr/bin/env python3
"""Screenshot the avatar viewer (flutter_app/web/companion/avatar3d.js) with the
built GLB, at phone and desktop sizes, through Playwright's bundled Chromium
(SwiftShader WebGL; `--no-sandbox` for this sandbox).

    python3 tools/avatar/shot.py [--out DIR]

Serves the repo root on 127.0.0.1 (random port) only for the duration of the
run; the GLB is personal-use and is never served anywhere else.
"""
import argparse, functools, http.server, json, os, socketserver, sys, threading, time

from playwright.sync_api import sync_playwright

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
VIEWPORTS = [('390x844', 390, 844, 3), ('1280x800', 1280, 800, 1)]
SHOTS = [
    # name, query, settle seconds
    ('idle-full', 'framing=full', 2.0),
    ('idle-upper', 'framing=upper', 2.0),
    ('talking', 'framing=upper&state=talking&speaking=0.8', 2.5),
    ('happy', 'framing=upper&mood=quietly_pleased', 3.0),
]


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()


Quiet.extensions_map.update({'.js': 'text/javascript', '.mjs': 'text/javascript', '.glb': 'model/gltf-binary', '.wasm': 'application/wasm'})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(REPO, 'assets', 'build', 'shots'))
    args = ap.parse_args()
    glb = os.path.join(REPO, 'assets', 'build', 'klukai.glb')
    if not os.path.exists(glb):
        sys.exit('assets/build/klukai.glb missing — run `node tools/avatar/build.mjs` first')
    os.makedirs(args.out, exist_ok=True)

    srv = socketserver.ThreadingTCPServer(('127.0.0.1', 0), functools.partial(Quiet, directory=REPO))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{srv.server_address[1]}/tools/avatar/test-page/index.html'

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'])
        for vname, w, h, dpr in VIEWPORTS:
            for name, query, settle in SHOTS:
                page = browser.new_page(viewport={'width': w, 'height': h}, device_scale_factor=dpr)
                logs = []
                page.on('console', lambda m: logs.append(f'{m.type}: {m.text}'))
                page.on('pageerror', lambda e: logs.append(f'pageerror: {e}'))
                t0 = time.time()
                page.goto(f'{base}?{query}')
                page.wait_for_function('window.__events && window.__events.length > 0', timeout=180000)
                events = page.evaluate('window.__events')
                page.wait_for_timeout(int(settle * 1000))
                path = os.path.join(args.out, f'{vname}-{name}.png')
                page.screenshot(path=path)
                errs = [l for l in logs if 'error' in l.lower() and 'GPU stall' not in l]
                results.append({'shot': path, 'events': events, 'errors': errs[:5], 'secs': round(time.time() - t0, 1)})
                print(json.dumps(results[-1]))
                page.close()
        browser.close()
    srv.shutdown()
    bad = [r for r in results if r['errors'] or not any(e == 'ready' for e in r['events'])]
    if bad:
        sys.exit(f'{len(bad)} shot(s) had errors')


if __name__ == '__main__':
    main()
