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

// Keep at most one prefetched model, matched by identity rather than timing.
export function createModelPrefetch() {
  let entry = null;
  return {
    warm(key, load) {
      if (entry?.key === key) return;
      const next = { key, promise: Promise.resolve().then(load) };
      entry = next;
      next.promise.catch(() => { if (entry === next) entry = null; });
    },
    take(key, load) {
      const promise = entry?.key === key ? entry.promise : Promise.resolve().then(load);
      entry = null;
      return promise;
    },
  };
}
