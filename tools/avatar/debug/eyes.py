#!/usr/bin/env python3
"""Eyes-under-the-brim check: every clip at N times, upper framing on the real stage layout,
face crops in a labelled contact sheet plus an iris-visibility score.

    python3 tools/avatar/debug/eyes.py OUT_PREFIX [--glb URL] [--size 390x844x2] [--times 8] [--guard on|off]

Each tile is labelled "clip t / camera elevation above the face's plane before→after the
guard (°) / iris %". The iris % is yellow-green iris pixels in the crop relative to the
config's best tile (100 = as visible as it ever gets; 0 = both hidden). Writes
OUT_PREFIX-sheet.jpg and OUT_PREFIX.json. Serialise SwiftShader runs (shared render lock).
"""
import argparse, functools, http.server, io, json, os, socketserver, threading
from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
CLIP_STATE = {
    'idle': 'idle', 'talking': 'talking', 'thinking': 'thinking', 'happy': 'happy', 'thankful': 'happy',
    'bashful': 'bashful', 'defeated': 'sad', 'salute': 'greet', 'sleep': 'sleep', 'excited': 'excited', 'nervous': 'flustered',
}


class Q(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


Q.extensions_map.update({'.js': 'text/javascript', '.mjs': 'text/javascript'})


def iris_pixels(img):
    n = 0
    for r, g, b in (img.get_flattened_data() if hasattr(img, 'get_flattened_data') else img.getdata()):
        if g > 150 and b < 120 and r > 100 and g - b > 80 and g >= r * 0.9:
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out')
    ap.add_argument('--glb', default='/assets/build/klukai.glb')
    ap.add_argument('--size', default='390x844x2')
    ap.add_argument('--times', type=int, default=8)
    ap.add_argument('--guard', default='on', choices=['on', 'off'])
    ap.add_argument('--clips', default='', help='comma-separated subset')
    args = ap.parse_args()
    w, h, dpr = args.size.split('x')
    w, h, dpr = int(w), int(h), float(dpr)
    srv = socketserver.ThreadingTCPServer(('127.0.0.1', 0), functools.partial(Q, directory=REPO))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    rows = []
    with sync_playwright() as p:
        b = p.chromium.launch(args=['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        pg = b.new_page(viewport={'width': w, 'height': h}, device_scale_factor=dpr)
        errs = []
        pg.on('pageerror', lambda e: errs.append(str(e)))
        pg.goto(f'http://127.0.0.1:{srv.server_address[1]}/tools/avatar/test-page/stage.html?newcss=1&glb={args.glb}')
        pg.wait_for_function('window.__events.length>0', timeout=120000)
        pg.wait_for_timeout(800)
        clips = pg.evaluate('[...av.debug.mixer._actions].map((a) => a.getClip().name).filter((n) => !n.startsWith("fidget_"))')
        clips = [c for c in dict.fromkeys(clips) if not args.clips or c in args.clips.split(',')]
        for clip in clips:
            tiles = []
            for i in range(args.times):
                dur = pg.evaluate(f'av.debug.mixer._actions.find((a) => a.getClip().name === {json.dumps(clip)}).getClip().duration')
                t = (i + 0.5) / args.times * dur
                r = pg.evaluate('([c, t, s, g]) => av.debug.poseAt(c, t, { state: s, guard: g })', [clip, t, CLIP_STATE.get(clip, 'idle'), args.guard == 'on'])
                ox, oy = pg.evaluate('(() => { const c = document.querySelector("canvas").getBoundingClientRect(); return [c.left, c.top]; })()')
                (ex, ey), (tx, ty) = (r['eyePx'][0] + ox, r['eyePx'][1] + oy), (r['headTopPx'][0] + ox, r['headTopPx'][1] + oy)
                rad = max(18.0, ((ex - tx) ** 2 + (ey - ty) ** 2) ** 0.5 * 1.05)
                cx, cy = ex, ey - 0.25 * rad
                box = {'x': max(0, cx - 1.25 * rad), 'y': max(0, cy - 1.25 * rad), 'width': 2.5 * rad, 'height': 2.5 * rad}
                box['width'] = min(box['width'], w - box['x'])
                box['height'] = min(box['height'], h - box['y'])
                if box['x'] >= w - 4 or box['y'] >= h - 4 or box['width'] < 8 or box['height'] < 8:
                    img = Image.new('RGB', (150, 150), (60, 0, 0))  # face off-screen
                else:
                    img = Image.open(io.BytesIO(pg.screenshot(clip=box))).convert('RGB').resize((150, 150))
                tiles.append({'t': round(t, 2), 'e0': round(r['eyeElevDeg'], 1), 'e1': round(r['eyeElevAfterDeg'], 1), 'lift': round(r['guardLiftDeg'], 1), 'iris': iris_pixels(img), 'img': img})
            rows.append((clip, tiles))
            print(clip, [(x['t'], x['e0'], x['e1'], x['iris']) for x in tiles])
        b.close()
    srv.shutdown()
    best = max(1, max((x['iris'] for c, ts in rows for x in ts if c != 'sleep'), default=1))  # sleep's side view is larger
    T, L = 150, 30
    S = Image.new('RGB', (90 + T * args.times, (T + L) * len(rows)), (12, 14, 20))
    d = ImageDraw.Draw(S)
    out = []
    for r, (clip, ts) in enumerate(rows):
        y = r * (T + L)
        d.text((4, y + T // 2), clip, fill=(255, 255, 0))
        for c, x in enumerate(ts):
            pct = round(100 * x['iris'] / best)
            S.paste(x['img'], (90 + c * T, y))
            col = (255, 80, 80) if pct < 25 else (255, 200, 80) if pct < 50 else (180, 255, 180)
            d.text((92 + c * T, y + T + 2), f"{x['t']}s {x['e0']}→{x['e1']}°", fill=(220, 220, 220))
            d.text((92 + c * T, y + T + 15), f"iris {pct}%", fill=col)
            out.append({'clip': clip, 't': x['t'], 'e0': x['e0'], 'e1': x['e1'], 'lift': x['lift'], 'iris_pct': pct})
    S.save(f'{args.out}-sheet.jpg', quality=88)
    json.dump(out, open(f'{args.out}.json', 'w'), indent=0)
    low = [o for o in out if o['iris_pct'] < 25 and o['clip'] != 'sleep']
    print(f'{args.out}-sheet.jpg', 'tiles', len(out), 'low-iris (<25%, excl. sleep):', len(low), [(o['clip'], o['t']) for o in low][:20])
    if errs:
        print('errors', errs[:5])


if __name__ == '__main__':
    main()
