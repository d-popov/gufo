// The Python box. This runs in a dedicated module worker (this Pyodide release does not
// support classic ones), and gufo-monitor serves this file with a CSP of its own:
// default-src 'none', connect-src 'none', script-src 'self'. So nothing in here, including
// the Python the model writes, can make a network request: not fetch, not XMLHttpRequest,
// not WebSocket, not through the prototype. It never sees the monitor's pages, cookies or
// storage either. Pyodide's own files reach it as messages from the chat page, and fetch is
// replaced before the runtime loads so that it answers only for those files.

import { loadPyodide } from '/static/pyodide/pyodide.mjs';

const BASE = '/static/pyodide/';
const MAX_OUTPUT_CHARS = 20000;
const MAX_FIGURES = 4;
const TYPES = { wasm: 'application/wasm', json: 'application/json', js: 'text/javascript', mjs: 'text/javascript' };

const pending = new Map();
let nextAsset = 0;

// Asks the page for one of Pyodide's files; resolves to its bytes.
function asset(name) {
  return new Promise((resolve, reject) => {
    const id = ++nextAsset;
    pending.set(id, { resolve, reject });
    postMessage({ type: 'asset', id, name });
  });
}

self.fetch = async (input) => {
  // Pyodide passes a string, a URL object or a Request.
  const url = new URL(input?.url ?? input, self.location.href);
  if (url.origin !== self.location.origin || !url.pathname.startsWith(BASE)) {
    throw new TypeError('there is no network in the Python box');
  }
  const name = url.pathname.slice(BASE.length);
  const buffer = await asset(name);
  return new Response(buffer, {
    status: 200, headers: { 'Content-Type': TYPES[name.split('.').pop()] || 'application/octet-stream' },
  });
};

let ready = false;
let pyodide;
let namespace;  // the model's variables, kept between runs; separate from the helper below

const booted = (async () => {
  const started = performance.now();
  pyodide = await loadPyodide({ indexURL: BASE });
  pyodide.runPython(`
import os
os.environ['MPLBACKEND'] = 'Agg'
import warnings
warnings.filterwarnings('ignore', message='FigureCanvasAgg is non-interactive')  # plt.show() on Agg

def __gufo_figures():
    """Every open matplotlib figure as base64 PNG, then closes them."""
    import sys
    pyplot = sys.modules.get('matplotlib.pyplot')
    if pyplot is None:
        return []
    import base64, io
    figures = []
    for number in pyplot.get_fignums()[:${MAX_FIGURES}]:
        buffer = io.BytesIO()
        pyplot.figure(number).savefig(buffer, format='png', dpi=100, bbox_inches='tight')
        figures.append(base64.b64encode(buffer.getvalue()).decode())
    pyplot.close('all')
    return figures
`);
  namespace = pyodide.globals.get('dict')();
  ready = true;
  postMessage({ type: 'ready', ms: Math.round(performance.now() - started) });
})().catch((error) => {
  postMessage({ type: 'bootfailed', error: String(error?.message || error).slice(0, 300) });
});

// A rejection nobody awaits while the runtime loads would leave the page waiting for a box
// that will never start; say so instead.
self.addEventListener('unhandledrejection', (event) => {
  if (!ready) postMessage({ type: 'bootfailed', error: String(event.reason?.message || event.reason).slice(0, 300) });
});

function figures() {
  try {
    const proxy = pyodide.globals.get('__gufo_figures')();
    const list = proxy.toJs();
    proxy.destroy();
    return list;
  } catch { return []; }
}

async function run({ id, code }) {
  await booted;
  const output = [];
  let size = 0;
  const collect = (text) => {
    if (size >= MAX_OUTPUT_CHARS) return;
    output.push(text);
    size += text.length + 1;
  };
  pyodide.setStdout({ batched: collect });
  pyodide.setStderr({ batched: collect });
  try {
    // Package loading is not the model's business: its messages stay out of the output.
    await pyodide.loadPackagesFromImports(code, { messageCallback: () => {}, errorCallback: collect });
    const value = await pyodide.runPythonAsync(code, { globals: namespace });
    let result = null;
    if (value !== undefined && value !== null) {
      result = String(value);
      if (typeof value.destroy === 'function') value.destroy();
    }
    postMessage({ type: 'result', id, ok: true, output: output.join('\n'), result, figures: figures() });
  } catch (error) {
    postMessage({
      type: 'result', id, ok: false, output: output.join('\n'), figures: figures(),
      error: String(error?.message || error).trim().split('\n').slice(-12).join('\n').slice(-1500),
    });
  }
}

self.onmessage = (event) => {
  const message = event.data;
  if (message.type === 'asset') {
    const waiting = pending.get(message.id);
    pending.delete(message.id);
    if (!waiting) return;
    if (message.error) waiting.reject(new Error(message.error)); else waiting.resolve(message.buffer);
  } else if (message.type === 'run') {
    run(message);
  }
};
