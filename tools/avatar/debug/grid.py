#!/usr/bin/env python3
"""Look-dev grid: rows = variants (inspect3d.html query strings), columns = views.

    python3 tools/avatar/debug/grid.py OUT.jpg 'label|query' ['label|query' ...]
        [--views face,upper,front,3q,back] [--glb /assets/build/dev/klukai.glb] [--size 360x480]

Example: grid.py /tmp/keys.jpg 'base|' 'front key|set=keyPos:0.9;2;2.2' 'warm|set=keyColor:#fff2e8'
Every row is rendered on the stage backdrop (bg=stage) with the idle clip frozen at t=1.5 s
unless its query says otherwise. Serialise SwiftShader renders (flock the shared render lock).
"""
import argparse, functools, http.server, os, socketserver, threading
from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
VIEWS = {
    'face': 'yaw=20&pitch=5&dist=0.6&ty=1.37', 'face0': 'yaw=0&pitch=3&dist=0.55&ty=1.38',
    'face-low': 'yaw=-30&pitch=-25&dist=0.6&ty=1.37', 'upper': 'yaw=0&pitch=3&dist=2.0&ty=1.18',
    'upper3q': 'yaw=-30&pitch=5&dist=2.0&ty=1.18', 'front': 'yaw=0&pitch=5', '3q': 'yaw=35&pitch=12&dist=2.6&ty=1.0',
    'back': 'yaw=180&pitch=5', 'hair-back': 'yaw=160&pitch=20&dist=1.1&ty=1.2', 'jacket-in': 'yaw=150&pitch=-35&dist=1.0&ty=0.95',
    'legs': 'yaw=30&pitch=0&dist=1.6&ty=0.5', 'sleeve': 'yaw=-70&pitch=-10&dist=0.9&ty=1.05',
}


class Q(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


Q.extensions_map.update({'.js': 'text/javascript', '.mjs': 'text/javascript'})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out')
    ap.add_argument('rows', nargs='+')
    ap.add_argument('--views', default='face,upper,front')
    ap.add_argument('--glb', default='/assets/build/klukai.glb')
    ap.add_argument('--size', default='360x480')
    args = ap.parse_args()
    W, H = (int(x) for x in args.size.split('x'))
    views = [v for v in args.views.split(',') if v]
    srv = socketserver.ThreadingTCPServer(('127.0.0.1', 0), functools.partial(Q, directory=REPO))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    rows = []
    with sync_playwright() as p:
        b = p.chromium.launch(args=['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        for row in args.rows:
            label, _, query = row.partition('|')
            query = query.replace('#', '%23')  # colours: a raw '#' would start the URL fragment
            if not any(x.startswith('t=') for x in query.split('&')):
                query = (query + '&t=1.5').lstrip('&')
            tiles = []
            for v in views:
                pg = b.new_page(viewport={'width': W, 'height': H})
                errs = []
                pg.on('pageerror', lambda e: errs.append(str(e)))
                pg.on('console', lambda m: errs.append(m.text[:300]) if m.type == 'error' else None)
                pg.goto(f'http://127.0.0.1:{srv.server_address[1]}/tools/avatar/test-page/inspect3d.html?{VIEWS.get(v, v)}&bg=stage&glb={args.glb}&{query}')
                pg.wait_for_function('window.__events.length>0', timeout=120000)
                pg.wait_for_timeout(1200)
                path = f'{args.out}.{len(rows)}.{len(tiles)}.png'
                pg.screenshot(path=path)
                tiles.append(path)
                if errs:
                    print(label, v, errs[:3])
                pg.close()
            rows.append((label, tiles))
        b.close()
    srv.shutdown()
    S = Image.new('RGB', (W * len(views), (H + 18) * len(rows)), (0, 0, 0))
    d = ImageDraw.Draw(S)
    for r, (label, tiles) in enumerate(rows):
        for c, pth in enumerate(tiles):
            S.paste(Image.open(pth).convert('RGB'), (c * W, r * (H + 18) + 18))
            os.remove(pth)
        d.text((4, r * (H + 18) + 3), label, fill=(255, 255, 0))
    S.save(args.out, quality=88)
    print(args.out)


if __name__ == '__main__':
    main()
