// Resource ownership during asynchronous appearance changes.
export function createAppearanceSwitcher({ build, commit }) {
  let epoch = 0, active = null, key = null, pending = null, disposed = false;
  return {
    get key() { return key; },
    select(id) {
      if (disposed) return Promise.resolve(false);
      if (pending?.id === id) return pending.promise;
      const ticket = ++epoch;
      pending?.controller.abort();
      pending = null;
      if (active && key === id) return Promise.resolve(true);
      const job = { id, promise: null, controller: new AbortController() };
      pending = job;
      job.promise = (async () => {
        let next;
        try {
          next = await build(id, job.controller.signal);
          if (disposed || ticket !== epoch) { next?.dispose(); return false; }
          if (!next?.ready) throw new Error('Appearance could not be loaded');
          commit(next, id);
          const old = active;
          active = next; key = id; next = null;
          old?.dispose();
          return true;
        } catch (error) {
          next?.dispose();
          if (disposed || ticket !== epoch) return false;
          throw error;
        } finally {
          if (pending === job) pending = null;
        }
      })();
      return job.promise;
    },
    dispose() { disposed = true; ++epoch; pending?.controller.abort(); active?.dispose(); active = null; pending = null; },
  };
}

export function awaitWithAbort(promise, signal) {
  if (!signal) return promise;
  return new Promise((resolve, reject) => {
    const abort = () => reject(new DOMException('Appearance superseded', 'AbortError'));
    if (signal.aborted) abort();
    else signal.addEventListener('abort', abort, { once: true });
    Promise.resolve(promise).then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
  });
}

// One model download per model id, shared by everyone who asks for it: the
// boot warm-up, the stage's load and any retry all get the same promise (and,
// once it lands, the same ArrayBuffer), whatever order they ask in. At most
// one model is kept, matched by id rather than timing: asking for another id
// drops the old one and aborts its download if it is still running. A failed
// download is forgotten, so the next ask fetches again. `load(signal)` gets
// the cache's own signal: one caller giving up never cancels a download
// another caller is waiting for (each caller bounds its own wait).
export function createModelCache() {
  let entry = null;
  function start(key, load) {
    const old = entry;
    const controller = new AbortController();
    const next = { key, controller, promise: Promise.resolve().then(() => load(controller.signal)) };
    entry = next;
    old?.controller.abort();
    next.promise.catch(() => { if (entry === next) entry = null; });
    return next;
  }
  return {
    get key() { return entry?.key ?? null; },
    /** Start fetching `key` unless it is already fetched or on its way. */
    warm(key, load) { if (entry?.key !== key) start(key, load); },
    /** The model for `key`: the shared download, or a new one. */
    get(key, load) { return (entry?.key === key ? entry : start(key, load)).promise; },
    /** Drop this download (a model that would not parse) so the next ask refetches. */
    forget(promise) { if (entry?.promise === promise) entry = null; },
    /** Drop everything (his session ended); a running download is aborted. */
    clear() { const old = entry; entry = null; old?.controller.abort(); },
  };
}
