#!/usr/bin/env python3
"""Real app/rig acceptance for the CC0 motion library; no provider calls."""
import argparse
import functools
import json
from pathlib import Path
import socketserver
import threading
from playwright.sync_api import sync_playwright
from camera_e2e import ROOT, Quiet, install_fixtures, reset, check_dock_layout
from presence_e2e import probe

NEW = ['wave', 'nod', 'confused', 'bow', 'dance_charleston', 'dance_groove', 'cheer', 'listening']
POSE_TIME = {'wave': 1.4, 'nod': .35, 'confused': 1.4, 'bow': 1.7,
             'dance_charleston': .6, 'dance_groove': .9, 'cheer': 1, 'listening': .5}


def choose(page, kind):
    page.evaluate('cameraTestAvatar.setPaused(false)')
    page.get_by_role('button', name='Choose animation', exact=True).click()
    assert page.locator('#animation-dialog').evaluate('el => el.open')
    page.locator(f'[data-reaction="{kind}"]').click()
    assert not page.locator('#animation-dialog').evaluate('el => el.open')


def ready(page):
    page.wait_for_function('window.cameraTestAvatar?.ready', timeout=90000)
    assert page.evaluate('cameraTestAvatar.getMotionStatus()') == 'ready', probe(page)
    assert len(page.evaluate('cameraTestAvatar.getReactions()')) == 16


def check_picker(page, screenshot):
    page.get_by_role('button', name='Choose animation', exact=True).click()
    dialog = page.locator('#animation-dialog')
    bounds = dialog.bounding_box()
    assert bounds and bounds['x'] >= 0 and bounds['y'] >= 0, bounds
    assert bounds['x'] + bounds['width'] <= page.viewport_size['width'], bounds
    assert bounds['y'] + bounds['height'] <= page.viewport_size['height'], bounds
    assert dialog.locator('[data-reaction]').count() == 16
    page.screenshot(path=str(screenshot))
    last = dialog.locator('[data-reaction]').last
    last.scroll_into_view_if_needed()
    assert last.is_visible() and last.bounding_box()['height'] >= 44
    page.keyboard.press('Escape')
    assert not dialog.evaluate('el => el.open')


def run(browser, url, out, glb, mobile):
    label = 'phone' if mobile else 'desktop'
    context = browser.new_context(viewport={'width': 390 if mobile else 1280, 'height': 844 if mobile else 800},
                                  has_touch=mobile, is_mobile=mobile)
    page = context.new_page()
    _, errors = install_fixtures(page, glb, presence=True)
    result = {'viewport': label, 'appearances': []}
    try:
        page.goto(url); page.locator('#wake').click(); ready(page)
        reset(page); check_dock_layout(page)
        check_picker(page, out / f'{label}-animation-menu.png')
        for model in ['default', 'test-variant']:
            if model != 'default':
                page.get_by_role('button', name='Choose 3D appearance', exact=True).click()
                page.locator('#appearance-select').select_option(model)
                page.wait_for_function("JSON.parse(localStorage.getItem('klukai_companion_prefs')).appearance === 'test-variant'")
                ready(page)
            motions = []
            for kind in NEW:
                choose(page, kind)
                assert probe(page)['reaction']['kind'] == kind, probe(page)
                page.evaluate('(t) => cameraTestAvatar.debug.run(t)', POSE_TIME[kind])
                if kind in ['wave', 'nod', 'confused', 'bow', 'dance_charleston', 'dance_groove']:
                    page.screenshot(path=str(out / f'{label}-{model}-{kind}.png'))
                measured = page.evaluate("""() => {
                  const av=cameraTestAvatar, {THREE,root}=av.debug;
                  const bones=['Head','LeftHand','RightHand','LeftFoot','RightFoot','Hips'].map(n=>root.getObjectByName('mixamorig'+n));
                  const samples=[];
                  for(let i=0;i<28;i++) {
                    av.debug.run(.18); root.updateMatrixWorld(true);
                    samples.push(bones.map(b=>({p:b.getWorldPosition(new THREE.Vector3()).toArray(),q:b.getWorldQuaternion(new THREE.Quaternion()).toArray()})));
                  }
                  let travel=0,angle=0;
                  for(const a of samples)for(const b of samples)for(let i=0;i<a.length;i++) {
                    travel=Math.max(travel,Math.hypot(...a[i].p.map((x,j)=>x-b[i].p[j])));
                    angle=Math.max(angle,new THREE.Quaternion().fromArray(a[i].q).angleTo(new THREE.Quaternion().fromArray(b[i].q)));
                  }
                  return {travel,angle,ended:!av.debug.probe().reaction,state:av.debug.probe().state};
                }""")
                assert measured['travel'] > .006 or measured['angle'] > .05, (kind, measured)
                assert measured['ended'] and measured['state'] == 'idle', (kind, measured)
                motions.append({'id': kind, **measured})
                page.wait_for_timeout(2700)
            # Speech takes ownership immediately even during a newly imported dance.
            choose(page, 'dance_charleston')
            page.evaluate('cameraTestAvatar.setSpeaking(.8); cameraTestAvatar.debug.run(.5)')
            assert probe(page)['reaction'] is None
            assert probe(page)['mouthOpen'] > .1
            page.evaluate('cameraTestAvatar.setSpeaking(0); cameraTestAvatar.debug.run(2)')
            assert probe(page)['mouthOpen'] < .001
            assert 'motion_listening' in probe(page)['passive']['available']
            result['appearances'].append({'model': model, 'motions': motions, 'speech_preemption': True})
        page.emulate_media(reduced_motion='reduce')
        page.wait_for_function('cameraTestAvatar.debug.probe().reducedMotion')
        page.evaluate('cameraTestAvatar.debug.run(40)')
        assert probe(page)['passive']['active'] is None
        page.evaluate('cameraTestAvatar.dispose()')
        assert not errors, errors
        result.update(status='passed', page_errors=errors, disposal=True, reduced_motion=True)
        return result
    except Exception:
        page.screenshot(path=str(out / f'{label}-failure.png'))
        (out / f'{label}-errors.json').write_text(json.dumps(errors))
        raise
    finally:
        context.close()


def fallback_and_abort(browser, url, glb):
    context=browser.new_context(viewport={'width':390,'height':844})
    page=context.new_page(); _,errors=install_fixtures(page,glb,presence=True)
    try:
        pattern='**/motions/everyday-playful.json*'
        page.route(pattern,lambda route:route.fulfill(json={'version':0}))
        page.goto(url);page.locator('#wake').click()
        page.wait_for_function('window.cameraTestAvatar?.ready',timeout=90000)
        assert page.evaluate('cameraTestAvatar.getMotionStatus()')=='unavailable'
        assert len(page.evaluate('cameraTestAvatar.getReactions()'))==8
        choose(page,'salute');assert probe(page)['reaction']['kind']=='salute'
        page.unroute(pattern)
        page.get_by_role('button',name='Choose 3D appearance',exact=True).click()
        page.locator('#appearance-select').select_option('test-variant')
        page.wait_for_function("JSON.parse(localStorage.getItem('klukai_companion_prefs')).appearance==='test-variant'")
        ready(page)
        assert not errors,errors
    finally:context.close()
    context=browser.new_context(viewport={'width':390,'height':844})
    page=context.new_page();_,errors=install_fixtures(page,glb,presence=True); held=[]
    try:
        page.route('**/motions/everyday-playful.json*',lambda route:held.append(route))
        page.goto(url);page.locator('#wake').click()
        for _ in range(300):
            if held:break
            page.wait_for_timeout(100)
        assert held,'motion request never started'
        page.get_by_role('button',name='Choose 3D appearance',exact=True).click()
        page.locator('#appearance-select').select_option('test-variant')
        held[0].fulfill(path=str(ROOT/'flutter_app/web/companion/motions/everyday-playful.json'),content_type='application/json')
        page.wait_for_function("JSON.parse(localStorage.getItem('klukai_companion_prefs')).appearance==='test-variant'",timeout=90000)
        ready(page);assert not errors,errors
        return {'invalid_pack_fallback_and_retry':True,'superseded_load':True,'page_errors':errors}
    finally:context.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,default=Path('/tmp/klukai-library-check'))
    parser.add_argument('--viewport',choices=['desktop','phone','all'],default='all')
    parser.add_argument('--base-url')
    args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    glb=(ROOT/'assets/build/klukai.glb').read_bytes()
    with socketserver.ThreadingTCPServer(('127.0.0.1',0),functools.partial(Quiet,directory=str(ROOT))) as server:
        server.daemon_threads=True;threading.Thread(target=server.serve_forever,daemon=True).start()
        with sync_playwright() as p:
            browser=p.chromium.launch(args=['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist'])
            try:
                url=args.base_url or f'http://127.0.0.1:{server.server_address[1]}/flutter_app/web/companion.html'
                results=[]
                for mobile in ([False,True] if args.viewport=='all' else [args.viewport=='phone']):
                    result=run(browser,url,args.out,glb,mobile);results.append(result)
                    print(json.dumps(result),flush=True)
                    (args.out/'results.json').write_text(json.dumps(results,indent=2)+'\n')
                fallback=fallback_and_abort(browser,url,glb)
                (args.out/'fallback.json').write_text(json.dumps(fallback,indent=2)+'\n')
                print(json.dumps(fallback),flush=True)
            finally:browser.close()


if __name__=='__main__':main()
