#!/usr/bin/env python3
"""Measure avatar3d.js load time, FPS and renderer stats in Playwright Chromium
(SwiftShader software GL, a pessimistic proxy for an iPhone GPU).

    python3 tools/avatar/perf.py [--glb /assets/build/klukai.glb] [--throttle 4] [--runs 2]

`--throttle N` slows the CPU N× (Chrome DevTools emulation) to approximate a
phone. Prints one JSON line per run.
"""
import argparse, functools, http.server, json, os, socketserver, threading
from playwright.sync_api import sync_playwright

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


Quiet.extensions_map.update({'.js': 'text/javascript', '.glb': 'model/gltf-binary', '.wasm': 'application/wasm'})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--glb', default='/assets/build/klukai.glb')
    ap.add_argument('--throttle', type=float, default=4)
    ap.add_argument('--runs', type=int, default=2)
    ap.add_argument('--framing', default='upper')
    ap.add_argument('--size', default='390x844x3')
    args = ap.parse_args()
    w, h, dpr = (float(x) for x in args.size.split('x'))
    srv = socketserver.ThreadingTCPServer(('127.0.0.1', 0), functools.partial(Quiet, directory=REPO))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{srv.server_address[1]}/tools/avatar/test-page/perf.html?glb={args.glb}&framing={args.framing}'
    with sync_playwright() as p:
        b = p.chromium.launch(args=['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        for _ in range(args.runs):
            ctx = b.new_context(viewport={'width': int(w), 'height': int(h)}, device_scale_factor=dpr)
            page = ctx.new_page()
            if args.throttle and args.throttle > 1:
                cdp = ctx.new_cdp_session(page)
                cdp.send('Emulation.setCPUThrottlingRate', {'rate': args.throttle})
            page.goto(url)
            page.wait_for_function('window.__perf', timeout=300000)
            print(json.dumps({'throttle': args.throttle, **page.evaluate('window.__perf')}))
            ctx.close()
        b.close()
    srv.shutdown()


if __name__ == '__main__':
    main()
