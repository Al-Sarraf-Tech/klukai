#!/usr/bin/env python3
"""Render the live viewer (avatar3d.js) from many angles on a contrasting
backdrop: python3 tools/avatar/debug/views.py OUT_PREFIX [glb_path]"""
import sys, os, threading, functools, http.server, socketserver
from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
VIEWS = [
    ('front', 'yaw=0&pitch=5'), ('back', 'yaw=180&pitch=5'), ('left', 'yaw=-90&pitch=5'), ('right', 'yaw=90&pitch=5'),
    ('3q-above', 'yaw=35&pitch=35'), ('3q-below', 'yaw=-35&pitch=-30&ty=1.0'),
    ('face', 'yaw=20&pitch=5&dist=0.6&ty=1.37'), ('face-low', 'yaw=-30&pitch=-25&dist=0.6&ty=1.37'),
    ('hair-back', 'yaw=160&pitch=20&dist=1.1&ty=1.2'), ('jacket-in', 'yaw=150&pitch=-35&dist=1.0&ty=0.95'),
    ('sleeve', 'yaw=-70&pitch=-10&dist=0.9&ty=1.05'), ('legs', 'yaw=30&pitch=0&dist=1.6&ty=0.5'),
]
class Q(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
Q.extensions_map.update({'.js': 'text/javascript'})
out = sys.argv[1]; glb = sys.argv[2] if len(sys.argv) > 2 else '/assets/build/klukai.glb'
srv = socketserver.ThreadingTCPServer(('127.0.0.1', 0), functools.partial(Q, directory=REPO)); srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
tiles = []
with sync_playwright() as p:
    b = p.chromium.launch(args=['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
    for name, q in VIEWS:
        pg = b.new_page(viewport={'width': 420, 'height': 560})
        errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
        pg.goto(f'http://127.0.0.1:{srv.server_address[1]}/tools/avatar/test-page/inspect3d.html?{q}&glb={glb}')
        pg.wait_for_function('window.__events.length>0', timeout=120000); pg.wait_for_timeout(1500)
        path = f'{out}-{name}.png'; pg.screenshot(path=path); tiles.append((name, path))
        if errs: print(name, errs)
        pg.close()
    b.close()
srv.shutdown()
W, H = 420, 560
S = Image.new('RGB', (W * 6, (H + 18) * 2), (0, 0, 0)); d = ImageDraw.Draw(S)
for i, (n, pth) in enumerate(tiles):
    x = (i % 6) * W; y = (i // 6) * (H + 18)
    S.paste(Image.open(pth).convert('RGB'), (x, y + 18)); d.text((x + 4, y + 3), n, fill=(255, 255, 0))
S.save(f'{out}-sheet.jpg', quality=85); print(f'{out}-sheet.jpg')
