import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createAppearanceSwitcher, createModelPrefetch } from '../../../flutter_app/web/companion/appearance.js';
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
test('prefetch is consumed only by its own model key; stale rejection cannot clear newer entry',async()=>{
 const p=createModelPrefetch();const a=deferred();let loads=0;
 p.warm('a',()=>a.promise);p.warm('b',async()=>{loads++;return 'B';});
 a.reject(Error('old'));await Promise.resolve();await Promise.resolve();
 assert.equal(await p.take('b',()=>assert.fail('lost prefetch')),'B');assert.equal(loads,1);
 p.warm('a',async()=>'A');assert.equal(await p.take('b',async()=>'new B'),'new B');
 assert.equal(await p.take('a',async()=>'new A'),'new A');
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
