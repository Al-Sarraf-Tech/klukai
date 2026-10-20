import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createAppearanceSwitcher, createModelCache } from '../../../flutter_app/web/companion/appearance.js';
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b; }); return {promise,resolve,reject}; };
const view = () => ({ready:true, disposed:0,dispose(){this.disposed++;}});

test('newest selection wins; superseded and replaced views are disposed',async()=>{
 const a=deferred(),b=deferred(),one=view(),two=view(),commits=[];
 const s=createAppearanceSwitcher({build:id=>({a,b}[id].promise),commit:(v,id)=>commits.push(id)});
 const pa=s.select('a');const pb=s.select('b');b.resolve(two);await pb;a.resolve(one);await pa;
 assert.deepEqual(commits,['b']);assert.equal(one.disposed,1);assert.equal(two.disposed,0);
 s.dispose();assert.equal(two.disposed,1);
});
test('failed replacement preserves working avatar and can retry',async()=>{
 let fail=false;const one=view(),two=view();const commits=[];
 const s=createAppearanceSwitcher({build:async id=>{if(fail)throw Error('offline');return id==='a'?one:two;},commit:(v,id)=>commits.push(id)});
 await s.select('a');fail=true;await assert.rejects(s.select('b'),/offline/);assert.equal(one.disposed,0);assert.equal(s.key,'a');
 fail=false;await s.select('b');assert.equal(one.disposed,1);assert.deepEqual(commits,['a','b']);s.dispose();
});
test('returning to current choice cancels a pending replacement',async()=>{
 const one=view(),two=view(),d=deferred(),commits=[];
 const s=createAppearanceSwitcher({build:async id=>id==='a'?one:d.promise,commit:(v,id)=>commits.push(id)});
 await s.select('a');const pending=s.select('b');await s.select('a');d.resolve(two);await pending;
 assert.deepEqual(commits,['a']);assert.equal(two.disposed,1);assert.equal(one.disposed,0);s.dispose();
});
test('duplicate requests share one build, and disposal cancels in-flight completion',async()=>{
 const d=deferred(),v=view();let calls=0,commits=0;
 const s=createAppearanceSwitcher({build:()=>{calls++;return d.promise;},commit:()=>commits++});
 const a=s.select('x'),b=s.select('x');assert.equal(calls,1);s.dispose();d.resolve(v);await Promise.all([a,b]);assert.equal(commits,0);assert.equal(v.disposed,1);
});
test('a failed inert viewer is disposed, never committed',async()=>{
 const v={...view(),ready:false};const s=createAppearanceSwitcher({build:async()=>v,commit:()=>assert.fail('committed failed viewer')});
 await assert.rejects(s.select('x'));assert.equal(v.disposed,1);
});
test('model cache: one download per id, whichever of warm-up and load asks first',async()=>{
 const c=createModelCache();const d=deferred();let loads=0;const load=()=>{loads++;return d.promise;};
 const first=c.get('a',load);c.warm('a',load);const second=c.get('a',load);
 assert.equal(first,second);
 d.resolve('A');assert.equal(await first,'A');assert.equal(loads,1);
 c.warm('a',load);assert.equal(await c.get('a',load),'A');assert.equal(loads,1,'the downloaded bytes are kept for the next load');
 const w=createModelCache();let wl=0;w.warm('b',async()=>{wl++;return 'B';});w.warm('b',async()=>{wl++;return 'B2';});
 assert.equal(await w.get('b',()=>assert.fail('lost the warm-up')),'B');assert.equal(wl,1);assert.equal(w.key,'b');
});
test('model cache keeps one model: another id replaces it and aborts its running download',async()=>{
 const c=createModelCache();const a=deferred();let signalA,signalB;
 c.warm('a',(s)=>{signalA=s;return a.promise;});await Promise.resolve();
 assert.equal(await c.get('b',async(s)=>{signalB=s;return 'B';}),'B');
 assert.equal(signalA.aborted,true);assert.equal(signalB.aborted,false);
 a.reject(Error('old'));await Promise.resolve();await Promise.resolve();
 assert.equal(await c.get('b',()=>assert.fail('a stale rejection cleared the newer model')),'B');
 assert.equal(await c.get('a',async()=>'A again'),'A again');assert.equal(c.key,'a');
});
test('model cache forgets failures, unparseable bytes, and everything on clear',async()=>{
 const c=createModelCache();let n=0;
 await assert.rejects(c.get('a',async()=>{n++;throw Error('offline');}),/offline/);
 assert.equal(c.key,null);assert.equal(await c.get('a',async()=>{n++;return 'A';}),'A');assert.equal(n,2);
 const kept=c.get('a',()=>assert.fail('refetched'));c.forget(Promise.resolve('other'));assert.equal(c.get('a',()=>assert.fail('refetched')),kept);
 c.forget(kept);assert.equal(c.key,null);assert.equal(await c.get('a',async()=>{n++;return 'A2';}),'A2');assert.equal(n,3);
 const d=deferred();let signal;c.get('b',(s)=>{signal=s;return d.promise;}).catch(()=>{});await Promise.resolve();
 c.clear();assert.equal(signal.aborted,true);assert.equal(c.key,null);
});

test('superseding a load aborts it immediately, before its result settles',async()=>{
 const d=deferred();let signal;
 const s=createAppearanceSwitcher({build:async(id,abort)=>{if(id==='b'){signal=abort;return d.promise;}return view();},commit:()=>{}});
 await s.select('a');const p=s.select('b');await s.select('a');assert.equal(signal.aborted,true);d.resolve(view());await p;s.dispose();
});

test('aborted prefetch wait rejects promptly even if download never settles',async()=>{
 const {awaitWithAbort}=await import('../../../flutter_app/web/companion/appearance.js');
 const d=deferred(),c=new AbortController();const pending=awaitWithAbort(d.promise,c.signal);
 c.abort();await assert.rejects(pending,{name:'AbortError'});
 d.reject(Error('late download error'));await Promise.resolve();
 assert.equal(await awaitWithAbort(Promise.resolve('ready'),new AbortController().signal),'ready');
});
