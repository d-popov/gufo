'use strict';

// Run with node tests/tools/pybox_test.js. The chat's Python tool with a fake worker: how it
// serves the box's files, what it tells the model, and what it does when the code runs away,
// the user presses Stop, or the runtime will not start.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('tools/monitor/static/pybox.js', 'utf8');
const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

// A box whose worker answers by `script`: (message, worker) => reply data, or undefined to stay silent.
function load({ script = () => undefined, boot = { type: 'ready' }, request, bootMs = 2000, runMs = 2000 } = {}) {
  const workers = [];
  const requests = [];
  class FakeWorker {
    constructor() {
      this.sent = [];
      this.terminated = false;
      workers.push(this);
      if (boot) setTimeout(() => this.reply(boot), 0);
    }

    reply(data) { if (!this.terminated) this.onmessage({ data }); }

    postMessage(message, transfer) {
      this.sent.push({ message, transfer });
      if (message.type !== 'run') return;
      const data = script(message, this);
      if (data) setTimeout(() => this.reply({ type: 'result', id: message.id, ...data }), 0);
    }

    terminate() { this.terminated = true; }
  }
  const window = {};
  vm.runInNewContext(source, { window, setTimeout, clearTimeout, Promise, Object });
  const python = window.GufoPython.create({
    makeWorker: () => new FakeWorker(),
    request: async (url) => {
      requests.push(url);
      return request ? request(url) : { ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(8) };
    },
    bootMs, runMs,
  });
  return { python, workers, requests, tool: window.GufoPython.TOOL };
}

(async () => {
  // A run: what the model reads is the output, the last value and a note about figures; the
  // figures themselves go to the thread. The same box serves the next call.
  {
    const { python, workers } = load({ script: (message) => ({
      ok: true, output: 'hello\nworld\n', result: '4', figures: message.code.includes('plot') ? ['AAAA', 'BBBB'] : [],
    }) });
    const first = await python.run({ code: 'print("hello"); 2 + 2' });
    assert.deepEqual([first.ok, first.text, first.figures], [true, 'hello\nworld\nResult: 4', []]);
    const second = await python.run({ code: 'plot()' });
    assert.equal(second.ok, true);
    assert.match(second.text, /2 figures drawn and shown to the user \(you cannot see them\)\.$/);
    assert.deepEqual(second.figures, ['data:image/png;base64,AAAA', 'data:image/png;base64,BBBB']);
    for (const figure of second.figures) assert.match(figure, /^data:image\/png;base64,[A-Za-z0-9+/=]+$/, 'an <img> can show it');
    assert.equal(workers.length, 1, 'variables live in one box, so one box is reused');
    assert.deepEqual(workers[0].sent.filter((s) => s.message.type === 'run').map((s) => s.message.id), [1, 2]);
    assert.equal(python.state, 'ready');
  }

  // Output with nothing in it, a failing run, and oversized figures.
  {
    const { python } = load({ script: (message) => {
      if (message.code === 'quiet') return { ok: true, output: '', result: null, figures: [] };
      if (message.code === 'broken') return { ok: false, output: 'before\n', error: 'ZeroDivisionError: division by zero', figures: [] };
      return { ok: true, output: '', result: null, figures: ['x'.repeat(3000001), 'ok'] };
    } });
    assert.equal((await python.run({ code: 'quiet' })).text, '(no output)');
    const broken = await python.run({ code: 'broken' });
    assert.deepEqual([broken.ok, broken.text], [false, 'before\nError: ZeroDivisionError: division by zero']);
    const big = await python.run({ code: 'figs' });
    assert.deepEqual(big.figures, ['data:image/png;base64,ok']);
    assert.match(big.text, /1 figure drawn[^\n]*\n1 figure was too large to show\.$/);
  }

  // Long output is cut for the model.
  {
    const { python } = load({ script: () => ({ ok: true, output: 'x'.repeat(50000), result: null, figures: [] }) });
    const text = (await python.run({ code: 'spam' })).text;
    assert.ok(text.length < 6200, text.length);
    assert.match(text, /\[output cut at 6,000 characters\]$/);
  }

  // Calls with nothing to run never start the box.
  {
    const { python, workers } = load();
    for (const args of [undefined, {}, { code: '' }, { code: '  \n' }, { code: 42 }, { code: 'x'.repeat(20001) }]) {
      const result = await python.run(args);
      assert.equal(result.ok, false);
      assert.match(result.text, /^Error: /);
    }
    assert.equal(workers.length, 0);
    assert.equal(python.state, 'cold');
  }

  // The box asks for its files by name; only Pyodide's files are fetched, and a failure is
  // answered, not left hanging.
  {
    const { python, workers, requests } = load({ request: async (url) => {
      if (url.endsWith('missing.whl')) return { ok: false, status: 404 };
      if (url.endsWith('boom.whl')) throw new Error('network down');
      return { ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(16) };
    } });
    python.warm();
    await tick(5);
    const box = workers[0];
    for (const [id, name] of [[1, 'pyodide.asm.wasm'], [2, 'numpy-2.4.6-cp314-cp314-pyemscripten_2026_0_wasm32.whl'],
      [3, '../../etc/passwd'], [4, 'a/b'], [5, ''], [6, 'missing.whl'], [7, 'boom.whl']]) {
      box.reply({ type: 'asset', id, name });
    }
    await tick(10);
    assert.deepEqual(requests, ['/static/pyodide/pyodide.asm.wasm',
      '/static/pyodide/numpy-2.4.6-cp314-cp314-pyemscripten_2026_0_wasm32.whl',
      '/static/pyodide/missing.whl', '/static/pyodide/boom.whl'], 'traversal and odd names are never requested');
    const answers = Object.fromEntries(box.sent.filter((s) => s.message.type === 'asset').map((s) => [s.message.id, s]));
    assert.equal(answers[1].message.buffer.byteLength, 16);
    assert.equal(answers[1].transfer[0], answers[1].message.buffer, 'bytes are moved, not copied');
    assert.equal(answers[2].message.buffer.byteLength, 16);
    assert.match(answers[3].message.error, /not a Pyodide file/);
    assert.match(answers[4].message.error, /not a Pyodide file/);
    assert.match(answers[5].message.error, /not a Pyodide file/);
    assert.equal(answers[6].message.error, 'HTTP 404');
    assert.equal(answers[7].message.error, 'network down');
  }

  // Code that never ends: the box is killed at the limit, the model is told the variables are
  // gone, and the next call gets a fresh box.
  {
    const { python, workers } = load({ runMs: 80, script: (message) => (message.code === 'forever' ? undefined : { ok: true, output: '', result: '4', figures: [] }) });
    const stuck = await python.run({ code: 'forever' });
    assert.equal(stuck.ok, false);
    assert.match(stuck.text, /^Error: timed out after 0\.08 s\. The box was reset and its variables are lost$/);
    assert.equal(workers[0].terminated, true);
    assert.equal(python.state, 'cold');
    const after = await python.run({ code: '2+2' });
    assert.equal(after.text, 'Result: 4');
    assert.equal(workers.length, 2);
  }

  // Stop: the box is killed, the abort is rethrown (not turned into a tool error), and the next
  // call works. An abort before anything starts never starts the box.
  {
    const { python, workers } = load({ script: (message) => (message.code === 'forever' ? undefined : { ok: true, output: '', result: '4', figures: [] }) });
    const stop = new AbortController();
    const pending = python.run({ code: 'forever' }, stop.signal);
    setTimeout(() => stop.abort(), 20);
    await assert.rejects(pending, { name: 'AbortError' });
    assert.equal(workers[0].terminated, true);
    assert.equal((await python.run({ code: '2+2' })).text, 'Result: 4');
    const early = new AbortController();
    early.abort();
    await assert.rejects(python.run({ code: '1' }, early.signal), { name: 'AbortError' });
    assert.equal(workers.length, 2, 'no box for a call that was already stopped');
  }

  // The runtime cannot start (not installed, or out of time): the model is told, and a later call tries again.
  {
    const failing = load({ boot: null });
    setTimeout(() => failing.workers[0]?.reply({ type: 'bootfailed', error: 'Failed to fetch /static/pyodide/pyodide.asm.wasm: HTTP 404' }), 20);
    const missing = await failing.python.run({ code: '1' });
    assert.equal(missing.ok, false);
    assert.match(missing.text, /not installed on the monitor \(run tools\/monitor\/fetch-pyodide\.py\)/);
    assert.equal(failing.workers[0].terminated, true);

    const slow = load({ boot: null, bootMs: 50 });
    const late = await slow.python.run({ code: '1' });
    assert.match(late.text, /^Error: the Python box did not start in time/);
    assert.equal(slow.workers[0].terminated, true);
    await slow.python.run({ code: '1' });
    assert.equal(slow.workers.length, 2, 'the next call starts a new box');

    const crashed = load({ boot: null });
    setTimeout(() => crashed.workers[0].onerror({}), 10);
    assert.match((await crashed.python.run({ code: '1' })).text, /failed to start/);
  }

  // Calls made together run one after the other in the one box.
  {
    const order = [];
    const { python, workers } = load({ script: (message) => { order.push(`start ${message.code}`); return { ok: true, output: message.code, result: null, figures: [] }; } });
    const results = await Promise.all([python.run({ code: 'a' }), python.run({ code: 'b' }), python.run({ code: 'c' })]);
    assert.deepEqual(results.map((r) => r.text), ['a', 'b', 'c']);
    assert.deepEqual(order, ['start a', 'start b', 'start c']);
    assert.equal(workers.length, 1);
  }

  // The real box is a module worker (this Pyodide refuses classic ones) at the monitor's own
  // path; with nothing answering it, the call gives up instead of hanging.
  {
    const made = [];
    class RealishWorker {
      constructor(...args) { made.push(args); this.terminated = false; }
      postMessage() {}
      terminate() { this.terminated = true; }
    }
    const window = {};
    vm.runInNewContext(source, { window, setTimeout, clearTimeout, Promise, Object, Worker: RealishWorker });
    const python = window.GufoPython.create({ bootMs: 30 });
    const result = await python.run({ code: '1' });
    assert.match(result.text, /^Error: the Python box did not start in time/);
    assert.equal(made.length, 1);
    assert.equal(made[0][0], '/static/pyworker.js');
    assert.equal(JSON.stringify(made[0][1]), '{"type":"module"}');
  }

  // What the model is offered.
  {
    const { tool } = load();
    assert.equal(tool.function.name, 'run_python');
    assert.equal(JSON.stringify(tool.function.parameters.required), '["code"]');
    assert.match(tool.function.description, /matplotlib/);
  }

  console.log('Python tool: runs, figures, limits, file hand-off, timeout, Stop, boot failures and queueing passed.');
})().catch((error) => { console.error(error); process.exitCode = 1; });
