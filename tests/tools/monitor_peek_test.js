'use strict';

// Run with node tests/tools/monitor_peek_test.js. The Request content section must never show
// content once it is switched off or the admin session has ended, even when a response that was
// already on its way arrives afterwards.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node(tag = 'div') {
  return {
    tag, children: [], hidden: false, checked: false, className: '', textContent: '',
    setAttribute() {}, addEventListener() {}, scrollIntoView() {},
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
  };
}

function text(item) {
  if (typeof item === 'string') return item;
  return (item.textContent ?? '') + item.children.map(text).join('');
}

function page() {
  const nodes = new Map();
  const get = (id) => {
    if (!nodes.has(id)) nodes.set(id, node());
    return nodes.get(id);
  };
  const pending = [];  // fetches still waiting: { signal, resolve }
  const context = vm.createContext({
    document: { getElementById: get, createElement: node, addEventListener() {} },
    window: { GufoAdmin: { load: () => Promise.resolve() } },
    fetch: (url, options) => new Promise((resolve, reject) => {
      options.signal.addEventListener = options.signal.addEventListener ?? (() => {});
      pending.push({ url, signal: options.signal, resolve, reject });
    }),
    requestAnimationFrame() {}, setInterval, clearInterval, setTimeout, clearTimeout,
    AbortController, Intl, Set, Map, console,
  });
  vm.runInContext(fs.readFileSync('tools/monitor/static/app.js', 'utf8'), context);
  return { get, pending, run: (code) => vm.runInContext(code, context) };
}

const reply = (status, body) => ({ status, ok: status === 200, json: async () => body });
const secret = { data: [{ request: 'r7', generation: 7, state: 'running', client: 'alice',
  prompt: 'secret question', output: 'secret answer', prompt_tokens: 3, generated_tokens: 2 }] };
const settle = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
  // Content is shown while the section is on.
  let p = page();
  p.run('setPeek(true)');
  assert.equal(p.pending.length, 1);
  p.pending[0].resolve(reply(200, secret));
  await settle();
  assert.match(text(p.get('peek-list')), /secret answer/);
  p.run('stopPeek()');

  // Switched off while a response is on its way: it is dropped, nothing is rendered.
  p = page();
  p.run('setPeek(true)');
  p.run('setPeek(false)');
  assert.equal(p.get('peek-on').checked, false);
  p.pending[0].resolve(reply(200, secret));
  await settle();
  assert.equal(text(p.get('peek-list')), '');
  assert.equal(p.pending[0].signal.aborted, true);
  assert.equal(p.pending.length, 1, 'no timer keeps polling');

  // The same when the body is the part that is late.
  p = page();
  p.run('setPeek(true)');
  let finishBody;
  p.pending[0].resolve({ status: 200, ok: true, json: () => new Promise((resolve) => { finishBody = resolve; }) });
  await settle();
  p.run('setPeek(false)');
  finishBody(secret);
  await settle();
  assert.equal(text(p.get('peek-list')), '');

  // The session ends: content leaves the page, the box unticks and a late response stays out.
  p = page();
  p.run('setPeek(true)');
  p.pending[0].resolve(reply(200, secret));
  await settle();
  assert.match(text(p.get('peek-list')), /secret question/);
  p.run('refreshPeek()');  // a poll in flight when the session ends
  p.run('sessionExpired()');
  assert.equal(text(p.get('peek-list')), '');
  assert.equal(p.get('peek-on').checked, false);
  p.pending[1].resolve(reply(200, secret));
  await settle();
  assert.equal(text(p.get('peek-list')), '');

  // Switching on again after being off works: the stale epoch does not block a new poll.
  p = page();
  p.run('setPeek(true)');
  p.run('setPeek(false)');
  p.run('setPeek(true)');
  p.pending[1].resolve(reply(200, secret));
  await settle();
  assert.match(text(p.get('peek-list')), /secret answer/);
  p.run('stopPeek()');
  console.log('monitor peek checks passed');
})().catch((error) => { console.error(error); process.exit(1); });
