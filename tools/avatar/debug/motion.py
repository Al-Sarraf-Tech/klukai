#!/usr/bin/env python3
"""Motion strips: the viewer advanced deterministically (debug.run, fixed dt, no real
time) through a state sequence and/or clip moments, one frame every --every seconds.
Rows = GLB × view (× clip moment), columns = time. Each tile shows the time, the state
and the smallest gap between a hair-spring tail and a body collider (cm; < 0 = inside).

    python3 tools/avatar/debug/motion.py OUT_PREFIX \
        [--glb /assets/build/dev/w2/klukai.glb,/assets/build/klukai.glb] [--views back3q,front3q] \
        [--seq idle:2,talking:2:0.7,happy:3,excited:3] [--clips nervous:0.2:1.4,excited:3.3:1.2] \
        [--every 0.25] [--size 300x400]

Writes OUT_PREFIX-sheet.jpg and OUT_PREFIX.json (per frame: spring mode, JS ms, clearance).
Serialise SwiftShader renders with the shared render lock.
"""
import argparse, functools, http.server, io, json, os, socketserver, threading
from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
VIEWS = {
    'back3q': 'yaw=150&pitch=8&dist=2.6&ty=1.0', 'hair': 'yaw=160&pitch=10&dist=1.7&ty=1.05', 'hair-side': 'yaw=115&pitch=5&dist=1.8&ty=1.0', 'back': 'yaw=180&pitch=5&dist=2.6&ty=1.0',
    'side': 'yaw=90&pitch=5&dist=2.6&ty=1.0', 'front3q': 'yaw=-30&pitch=5&dist=2.6&ty=1.0',
    'front': 'yaw=0&pitch=5&dist=2.6&ty=1.0', 'full-back3q': 'yaw=150&pitch=8&dist=3.6&ty=0.8',
    'full-side': 'yaw=100&pitch=5&dist=3.4&ty=0.75', 'floor': 'yaw=60&pitch=20&dist=2.6&ty=0.2',
}
CLIP_STATE = {'idle': 'idle', 'talking': 'talking', 'thinking': 'thinking', 'happy': 'happy', 'thankful': 'happy', 'bashful': 'bashful',
              'defeated': 'sad', 'salute': 'greet', 'sleep': 'sleep', 'excited': 'excited', 'nervous': 'flustered'}


class Q(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


Q.extensions_map.update({'.js': 'text/javascript', '.mjs': 'text/javascript'})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out')
    ap.add_argument('--glb', default='/assets/build/dev/w2/klukai.glb')
    ap.add_argument('--views', default='back3q')
    ap.add_argument('--seq', default='idle:2,talking:2:0.7,happy:3,excited:3')
    ap.add_argument('--clips', default='')
    ap.add_argument('--every', type=float, default=0.25)
    ap.add_argument('--tune', default='', help='JSON for debug.tuneSprings, e.g. {"long":{"drag":0.4}}; several separated by |')
    ap.add_argument('--size', default='300x400')
    ap.add_argument('--wrap', type=int, default=12, help='frames per sheet row')
    ap.add_argument('--scale', type=float, default=0.6, help='tile size relative to the viewport')
    args = ap.parse_args()
    W, H = (int(x) for x in args.size.split('x'))
    srv = socketserver.ThreadingTCPServer(('127.0.0.1', 0), functools.partial(Q, directory=REPO))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    rows, report = [], []
    with sync_playwright() as p:
        b = p.chromium.launch(args=['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        for glb in [g for g in args.glb.split(',') if g]:
            tag = 'w2' if '/w2/' in glb else os.path.basename(os.path.dirname(glb)) or 'live'
            for view in [v for v in args.views.split(',') if v]:
                pg = b.new_page(viewport={'width': W, 'height': H})
                errs = []
                pg.on('pageerror', lambda e: errs.append(str(e)))
                pg.goto(f'http://127.0.0.1:{srv.server_address[1]}/tools/avatar/test-page/inspect3d.html?{VIEWS.get(view, view)}&life=1&bg=stage&glb={glb}')
                pg.wait_for_function('window.__events.length>0', timeout=120000)
                pg.wait_for_timeout(300)
                tunes = [t for t in args.tune.split('|') if t] or ['']
                jobs = []
                if args.seq:
                    jobs.append(('seq', [s.split(':') for s in args.seq.split(',') if s]))
                for c in [c for c in args.clips.split(',') if c]:
                    name, t0, dur = c.split(':')
                    jobs.append((f'{name}@{t0}', [('clip', name, t0, dur)]))
                for tune, (label, segs) in [(t, j) for t in tunes for j in jobs]:
                    kind = label
                    if tune:
                        pg.evaluate(f'av.debug.tuneSprings({tune})')
                        label = f'{label} {tune}'
                    tiles = []
                    if kind == 'seq':
                        pg.evaluate("av.setState('idle'); av.debug.resetSprings(); av.debug.run(0.5)")
                        t = 0.0
                        for seg in segs:
                            state, secs = seg[0], float(seg[1])
                            spk = float(seg[2]) if len(seg) > 2 else 0.0
                            pg.evaluate(f"av.setState({json.dumps(state)})")
                            n = max(1, round(secs / args.every))
                            for _ in range(n):
                                # speech: syllable bursts at 4.5 Hz when a level is given
                                r = pg.evaluate(f"av.debug.run({args.every}, {{ speaking: {spk} > 0 ? (t) => ((t * 1000) % 222 < 110 ? {spk} : 0.05) : 0 }})")
                                t += args.every
                                tiles.append((f'{t:.2f}s {state}', r, Image.open(io.BytesIO(pg.screenshot())).convert('RGB')))
                    else:
                        _, name, t0, dur = segs[0]
                        pg.evaluate(f"av.debug.playClip({json.dumps(name)}, {float(t0)}, {{ state: {json.dumps(CLIP_STATE.get(name, 'idle'))} }}); av.debug.resetSprings(); av.debug.run(0.04)")
                        n = max(1, round(float(dur) / args.every))
                        for i in range(n):
                            r = pg.evaluate(f"av.debug.run({args.every})")
                            tiles.append((f'{name} {float(t0) + (i + 1) * args.every:.2f}s', r, Image.open(io.BytesIO(pg.screenshot())).convert('RGB')))
                    rows.append((f'{tag} {view} {label}', tiles))
                    for lab, r, _ in tiles:
                        sp = r.get('springs') or {}
                        cl = (sp.get('clearance') or {})
                        report.append({'row': f'{tag} {view} {label}', 'frame': lab, 'mode': sp.get('mode'), 'ms': sp.get('ms'), 'clear_cm': None if cl.get('min') is None else round(cl['min'] * 100, 1), 'at': cl.get('at')})
                if errs:
                    print('errors', errs[:3])
                pg.close()
        b.close()
    srv.shutdown()
    cols = min(args.wrap, max(len(t) for _, t in rows))
    TW, TH = int(W * args.scale), int(H * args.scale)
    lines = []  # (label or None, tiles) wrapped
    for label, tiles in rows:
        for k in range(0, len(tiles), cols):
            lines.append((label if k == 0 else None, tiles[k:k + cols]))
    S = Image.new('RGB', (TW * cols, sum((TH + 30) + (14 if l else 0) for l, _ in lines)), (10, 12, 18))
    d = ImageDraw.Draw(S)
    y = 0
    for label, tiles in lines:
        if label:
            d.text((4, y + 2), label, fill=(255, 255, 0)); y += 14
        for c, (lab, res, img) in enumerate(tiles):
            S.paste(img.resize((TW, TH)), (c * TW, y))
            cl = ((res.get('springs') or {}).get('clearance') or {}).get('min')
            txt = lab + ('' if cl is None else f' {cl * 100:.1f}cm')
            d.text((c * TW + 2, y + TH + 2), txt, fill=(255, 120, 120) if cl is not None and cl < 0 else (210, 210, 210))
        y += TH + 30
    S.save(f'{args.out}-sheet.jpg', quality=86)
    json.dump(report, open(f'{args.out}.json', 'w'), indent=0)
    worst = min((x for x in report if x['clear_cm'] is not None), key=lambda x: x['clear_cm'], default=None)
    ms = [x['ms'] for x in report if x['ms'] is not None]
    print(f'{args.out}-sheet.jpg rows {len(rows)} frames {sum(len(t) for _, t in rows)}',
          'worst clearance', worst and (worst['clear_cm'], worst['row'], worst['frame'], worst['at']),
          'spring ms (EMA) max', round(max(ms), 3) if ms else None)


if __name__ == '__main__':
    main()
