#!/usr/bin/env python3
"""Exercise the viewer's procedural life layers in a real browser (SwiftShader) and
print what happened: python3 tools/avatar/debug/life.py OUT_PREFIX [--glb URL]

Checks: look-at engages, nods follow speech onsets, blinks (random + phrase end), the
eased framing change (frames at 0 / 0.3 / 0.8 / 1.6 s) with the contact shadow in full
framing, sleep, live mood hysteresis, setTint, and prefers-reduced-motion (no drift/sway).
Screenshots: OUT_PREFIX-*.png. Serialise SwiftShader runs with the shared render lock.
"""
import argparse, functools, http.server, json, os, socketserver, threading
from playwright.sync_api import sync_playwright

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))


class Q(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


Q.extensions_map.update({'.js': 'text/javascript', '.mjs': 'text/javascript'})

# Records the probe every animation frame for `ms` while optionally driving setSpeaking with a
# syllable pattern (4.5 Hz bursts, then silence).
RECORD = """async ([ms, speak]) => {
  const out = []; const t0 = performance.now();
  await new Promise((resolve) => {
    const step = () => {
      const t = performance.now() - t0;
      if (speak) av.setSpeaking(t < ms * 0.6 ? ((t % 222) < 110 ? 0.75 : 0.06) : 0);
      const p = av.debug.probe(); p.t = Math.round(t); out.push(p);
      if (t < ms) requestAnimationFrame(step); else resolve();
    };
    requestAnimationFrame(step);
  });
  return out;
}"""


def summarize(name, rows):
    import math
    pitches = [r['facePitch'] for r in rows]
    blinks = sum(1 for a, b in zip(rows, rows[1:]) if (a['blink'] or 0) < 0.5 <= (b['blink'] or 0))
    nods = sum(1 for a, b in zip(rows, rows[1:]) if b['nods'] > a['nods'] or (b['nods'] == a['nods'] == 3 and False))
    print(json.dumps({
        'run': name, 'frames': len(rows), 'secs': rows[-1]['t'] / 1000,
        'lookW': round(rows[-1]['lookW'], 2), 'lookYawDeg': round(math.degrees(rows[-1]['lookYaw']), 1), 'lookPitchDeg': round(math.degrees(rows[-1]['lookPitch']), 1),
        'facePitchRangeDeg': round(math.degrees(max(pitches) - min(pitches)), 1), 'blinks': blinks,
        'maxTalkPresence': round(max(r['talkPresence'] for r in rows), 2), 'maxPushIn': round(max(r['pushIn'] for r in rows), 2),
        'armW': round(rows[-1]['armW'], 2), 'drift': [round(v, 4) for v in rows[-1]['drift']], 'swayAmp': round(rows[-1]['swayAmp'], 4),
        'state': rows[-1]['state'], 'clip': rows[-1]['clip'], 'fov': round(rows[-1]['fov'], 2), 'headJoint': rows[-1]['headJoint'],
    }))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out')
    ap.add_argument('--glb', default='/assets/build/klukai.glb')
    args = ap.parse_args()
    srv = socketserver.ThreadingTCPServer(('127.0.0.1', 0), functools.partial(Q, directory=REPO))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{srv.server_address[1]}/tools/avatar/test-page/stage.html?newcss=1&glb={args.glb}'
    with sync_playwright() as p:
        b = p.chromium.launch(args=['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        for motion in ('no-preference', 'reduce'):
            ctx = b.new_context(viewport={'width': 390, 'height': 844}, device_scale_factor=1, reduced_motion=motion)
            pg = ctx.new_page()
            errs = []
            pg.on('pageerror', lambda e: errs.append(str(e)))
            pg.on('console', lambda m: errs.append(m.text[:200]) if m.type == 'error' else None)
            pg.goto(base)
            pg.wait_for_function('window.__events.length>0', timeout=120000)
            tag = 'rm' if motion == 'reduce' else 'n'
            summarize(f'{tag}-idle', pg.evaluate(RECORD, [3000, False]))
            summarize(f'{tag}-talk', pg.evaluate(RECORD, [4000, True]))
            if motion == 'reduce':
                ctx.close()
                continue
            pg.screenshot(path=f'{args.out}-upper.png')
            # eased framing change + contact shadow
            pg.evaluate("av.setFraming('full')")
            for ms in (0, 300, 500, 800):
                if ms:
                    pg.wait_for_timeout(ms)
                pg.screenshot(path=f'{args.out}-toFull-{ms}.png')
            print(json.dumps({'run': 'full', **{k: v for k, v in pg.evaluate('av.debug.probe()').items() if k in ('shadowA', 'shadowVisible', 'fov', 'cam', 'target')}}))
            pg.evaluate("av.setState('sleep')")
            pg.wait_for_timeout(2500)
            pg.screenshot(path=f'{args.out}-sleep.png')
            print(json.dumps({'run': 'sleep', **{k: v for k, v in pg.evaluate('av.debug.probe()').items() if k in ('state', 'shadowA', 'blink', 'lookW', 'cam')}}))
            # live hysteresis: mood → mood inside 4 s is held; setState is immediate; leaving it is immediate
            seq = pg.evaluate("""async () => {
              const s = []; const w = (ms) => new Promise((r) => setTimeout(r, ms)); const p = () => { const x = av.debug.probe(); return [x.state, x.source, x.pending]; };
              av.setFraming('upper'); av.setMood('composed'); await w(4200); s.push(['composed', ...p()]);
              av.setMood('content'); s.push(['content', ...p()]);
              await w(800); av.setMood('curious'); s.push(['curious<4s', ...p()]);
              av.setState('thinking'); s.push(['setState thinking', ...p()]);
              await w(300); av.setMood('content'); s.push(['mood after thinking', ...p()]);
              await w(500); av.setMood('shy'); s.push(['shy<4s', ...p()]);
              await w(3600); s.push(['after hold', ...p()]);
              av.setMood('terrified'); s.push(['strong', ...p()]);
              av.setTint('#ff9fbf'); await w(1500); s.push(['tint', av.debug.probe().rim]);
              return s;
            }""")
            for row in seq:
                print('  ', row)
            pg.screenshot(path=f'{args.out}-end.png')
            if errs:
                print('errors:', errs[:5])
            ctx.close()
        b.close()
    srv.shutdown()


if __name__ == '__main__':
    main()
