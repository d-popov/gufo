'use strict';

// The chat's Python tool. The model's code runs in /static/pyworker.js, a worker that has no
// network (see there); this side starts it on first use, hands it Pyodide's files from
// /static/pyodide/ (signed-in users only), runs one call at a time with a time limit, and
// returns the text for the model and any matplotlib figures for the thread. Variables stay
// between calls until a timeout or Stop resets the box.

window.GufoPython = (() => {
  const WORKER_URL = '/static/pyworker.js';
  const ASSET_URL = '/static/pyodide/';
  const BOOT_MS = 180000;  // the first use downloads the runtime
  const RUN_MS = 30000;
  const MAX_CODE_CHARS = 20000;
  const OUTPUT_CHARS = 6000;
  const MAX_FIGURE_CHARS = 3000000;  // base64 characters of one PNG

  const TOOL = {
    type: 'function',
    function: {
      name: 'run_python',
      description: 'Run Python 3 in a sandbox and return what it printed. numpy, pandas and matplotlib are '
        + 'available; there is no network and no files are kept; variables stay between calls. Draw charts '
        + 'with matplotlib: every figure is shown to the user automatically (do not call plt.show()).',
      parameters: {
        type: 'object',
        properties: { code: { type: 'string', description: 'The Python code. Its last expression is returned.' } },
        required: ['code'],
      },
    },
  };

  function abortError() {
    return Object.assign(new Error('aborted'), { name: 'AbortError' });
  }

  function clip(text, max) {
    return text.length <= max ? text : `${text.slice(0, max)}\n[output cut at ${max.toLocaleString()} characters]`;
  }

  function create({
    makeWorker = () => new Worker(WORKER_URL, { type: 'module' }), request = (url) => fetch(url),
    bootMs = BOOT_MS, runMs = RUN_MS,
  } = {}) {
    let worker = null;     // the box; null before first use and after a reset
    let booting = null;    // { promise, resolve, reject } while it starts
    let current = null;    // the run in progress: { id, resolve }
    let state = 'cold';    // cold | booting | ready
    let queue = Promise.resolve();
    let nextRun = 0;

    // Answers the worker's request for one of Pyodide's files.
    async function serve({ id, name }) {
      const target = worker;
      const reply = (message, transfer = []) => { if (worker === target) target.postMessage(message, transfer); };
      if (!/^[\w.+-]+$/.test(String(name))) return reply({ type: 'asset', id, error: 'not a Pyodide file' });
      try {
        const response = await request(ASSET_URL + name);
        if (!response.ok) return reply({ type: 'asset', id, error: `HTTP ${response.status}` });
        const buffer = await response.arrayBuffer();
        reply({ type: 'asset', id, buffer }, [buffer]);
      } catch (error) {
        reply({ type: 'asset', id, error: String(error?.message || error) });
      }
    }

    function reset(reason) {
      worker?.terminate();
      worker = null;
      state = 'cold';
      booting?.reject(reason);
      booting = null;
      current?.resolve({ reset: true });
      current = null;
    }

    function onMessage(data) {
      if (data.type === 'asset') serve(data);
      else if (data.type === 'ready') { state = 'ready'; booting?.resolve(); }
      else if (data.type === 'bootfailed') reset(new Error(data.error || 'the Python box did not start'));
      else if (data.type === 'result' && current && data.id === current.id) {
        const done = current;
        current = null;
        done.resolve(data);
      }
    }

    function start() {
      if (worker) return booting?.promise ?? Promise.resolve();
      worker = makeWorker();
      state = 'booting';
      let resolve;
      let reject;
      const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
      promise.catch(() => {});  // reported by whoever awaits it
      booting = { promise, resolve, reject };
      worker.onmessage = (event) => onMessage(event.data);
      worker.onerror = () => reset(new Error('the Python box failed to start'));
      return promise;
    }

    function text(data, code) {
      const lines = [];
      if (data.output?.trim()) lines.push(clip(data.output.trimEnd(), OUTPUT_CHARS));
      if (data.ok && data.result != null) lines.push(`Result: ${clip(data.result, OUTPUT_CHARS)}`);
      if (!data.ok) lines.push(`Error: ${data.error}`);
      const kept = (data.figures ?? []).filter((figure) => figure.length <= MAX_FIGURE_CHARS);
      return { lines, figures: kept.map((figure) => `data:image/png;base64,${figure}`), code };
    }

    async function once(args, signal) {
      const code = typeof args?.code === 'string' ? args.code : '';
      if (!code.trim()) return { ok: false, text: 'Error: no code given', via: null };
      if (code.length > MAX_CODE_CHARS) return { ok: false, text: `Error: the code is longer than ${MAX_CODE_CHARS} characters`, via: null };
      if (signal?.aborted) throw abortError();

      const onAbort = () => reset(abortError());
      signal?.addEventListener('abort', onAbort, { once: true });
      let timer;
      const within = (promise, ms, message) => Promise.race([
        promise, new Promise((_, fail) => { timer = setTimeout(() => fail(new Error(message)), ms); })]);
      try {
        await within(start(), bootMs, 'the Python box did not start in time');
        const id = ++nextRun;
        const finished = new Promise((resolve) => { current = { id, resolve }; });
        worker.postMessage({ type: 'run', id, code });
        clearTimeout(timer);
        const data = await within(finished, runMs, `timed out after ${runMs / 1000} s`);
        if (data.reset) throw signal?.aborted ? abortError() : new Error('the Python box was reset');
        const { lines, figures } = text(data, code);
        const dropped = (data.figures?.length ?? 0) - figures.length;
        if (figures.length) {
          lines.push(`${figures.length} figure${figures.length === 1 ? '' : 's'} drawn and shown to the user (you cannot see them).`);
        }
        if (dropped > 0) lines.push(`${dropped} figure${dropped === 1 ? ' was' : 's were'} too large to show.`);
        return { ok: data.ok, text: lines.join('\n') || '(no output)', via: null, figures };
      } catch (error) {
        if (signal?.aborted) throw abortError();
        const timedOut = /timed out/.test(error.message);
        reset(error);
        const reason = /404/.test(error.message)
          ? 'the Python runtime is not installed on the monitor (run tools/monitor/fetch-pyodide.py)'
          : error.message;
        return {
          ok: false, via: null,
          text: `Error: ${reason}${timedOut ? '. The box was reset and its variables are lost' : ''}`,
        };
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener('abort', onAbort);
      }
    }

    // One run at a time: two calls in the same round queue up in the one box.
    function run(args, signal) {
      const result = queue.then(() => once(args, signal));
      queue = result.catch(() => {});
      return result;
    }

    return {
      run,
      warm: () => { start().catch(() => {}); },  // download the runtime now, not at the first call
      get state() { return state; },
    };
  }

  return { create, TOOL, ...create() };
})();
