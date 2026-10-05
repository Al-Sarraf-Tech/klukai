#!/usr/bin/env python3
"""Debug screenshots: python3 debug/snap.py out.png 'query' [w h]. Serves the repo root."""
import sys, threading, http.server, functools, socketserver, os, json
from playwright.sync_api import sync_playwright
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
class Q(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
    def end_headers(self):
        self.send_header('Cache-Control', 'no-store'); super().end_headers()
Q.extensions_map['.mjs'] = 'text/javascript'; Q.extensions_map['.js'] = 'text/javascript'
srv = socketserver.TCPServer(('127.0.0.1', 0), functools.partial(Q, directory=REPO)); port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
jobs = [a.split('::') for a in sys.argv[1:]]
with sync_playwright() as p:
    b = p.chromium.launch(args=['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
    for out, query, *wh in jobs:
        w, h = (int(wh[0]), int(wh[1])) if wh else (700, 900)
        pg = b.new_page(viewport={'width': w, 'height': h})
        msgs = []; pg.on('console', lambda m: msgs.append(m.text)); pg.on('pageerror', lambda e: msgs.append('ERR ' + str(e)))
        pg.goto(f'http://127.0.0.1:{port}/tools/avatar/debug/inspect.html?{query}')
        pg.wait_for_function('window.__done === true', timeout=120000)
        pg.screenshot(path=out)
        print(out, json.dumps(pg.evaluate('window.__info')), [m for m in msgs if 'GPU stall' not in m][:5])
        pg.close()
    b.close()
