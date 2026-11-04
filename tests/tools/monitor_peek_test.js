'use strict';

// Run with node tests/tools/monitor_peek_test.js. The Request content section must never show
// content once it is switched off or the admin session has ended, even when a response that was
// already on its way arrives afterwards.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// A fake element. A <pre> has a 100 px viewport and one pixel of content per character, and, like a
// browser, forgets how far it was scrolled when it is taken out of the page and put back.
function node(tag = 'div') {
  const item = {
    tag, children: [], hidden: false, checked: false, className: '', textContent: '',
    setAttribute() {}, addEventListener() {}, scrollIntoView() {},
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; items.forEach(forgetScroll); },
  };
  if (tag === 'pre') {
    let top = 0;
    item.clientHeight = 100;
    Object.defineProperty(item, 'scrollHeight', { get() { return Math.max(this.clientHeight, this.textContent.length); } });
    Object.defineProperty(item, 'scrollTop', {
      get() { return Math.min(top, Math.max(0, this.scrollHeight - this.clientHeight)); },
      set(value) { top = Math.max(0, value); },
    });
  }
  return item;
}

function forgetScroll(item) {
  if (item.tag === 'pre') item.scrollTop = 0;
  (item.children ?? []).forEach(forgetScroll);
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

  // A poll updates rows in place (so the admin's scroll and selection survive) and labels each
  // request queued, running, or finished with how it ended.
  p = page();
  const entry = (generation, state, extra = {}) => ({ ...secret.data[0], generation, request: `r${generation}`,
    state, ...extra });
  const poll = async (entries) => { p.run('refreshPeek()'); p.pending.at(-1).resolve(reply(200, { data: entries })); await settle(); };
  p.run('setPeek(true)');
  p.pending[0].resolve(reply(200, { data: [entry(2, 'running'), entry(3, 'queued'),
    entry(1, 'finished', { finish: 'length' })] }));
  await settle();
  const list = p.get('peek-list');
  const rows = [...list.children];
  assert.equal(rows.length, 3);
  assert.deepEqual(rows.map((row) => text(row.children[0].children[0])), ['running', 'queued', 'finished · length']);
  let replaced = 0;
  const replaceChildren = list.replaceChildren.bind(list);
  list.replaceChildren = (...items) => { replaced += 1; replaceChildren(...items); };
  await poll([entry(2, 'running', { output: 'secret answer, longer' }), entry(3, 'running'),
    entry(1, 'finished', { finish: 'length' })]);
  assert.equal(replaced, 0, 'an unchanged set of requests leaves the list alone');
  assert.deepEqual([...list.children], rows, 'the same nodes are reused');
  assert.match(text(rows[0]), /secret answer, longer/);
  assert.equal(text(rows[1].children[0].children[0]), 'running');
  await poll([entry(4, 'queued'), entry(2, 'running')]);
  assert.equal(replaced, 1, 'a changed set of requests rebuilds the list once');
  assert.equal(list.children[1], rows[0], 'a request still listed keeps its node');
  p.run('stopPeek()');
  // Scrolling. A long output grows while the admin reads it.
  p = page();
  const outputOf = (row) => row.children[2].children[1];
  const promptOf = (row) => row.children[1].children[1];
  const long = (n) => 'x'.repeat(n);
  p.run('setPeek(true)');
  p.pending[0].resolve(reply(200, { data: [entry(2, 'running', { output: long(50) }),
    entry(3, 'running', { output: long(500), prompt: long(500) })] }));
  await settle();
  const view = p.get('peek-list');
  const [first, second] = [...view.children];
  await poll([entry(2, 'running', { output: long(400) }), entry(3, 'running', { output: long(500), prompt: long(500) })]);
  assert.equal(outputOf(first).scrollTop, 300, 'an output too short to scroll follows its end once it can scroll');
  await poll([entry(2, 'running', { output: long(700) }), entry(3, 'running', { output: long(500), prompt: long(500) })]);
  assert.equal(outputOf(first).scrollTop, 600, 'an output at its end keeps following it');
  outputOf(second).scrollTop = 120;
  promptOf(second).scrollTop = 80;
  await poll([entry(2, 'running', { output: long(900) }), entry(3, 'running', { output: long(800), prompt: long(600) })]);
  assert.equal(outputOf(second).scrollTop, 120, 'an output the admin scrolled up stays where it is');
  assert.equal(promptOf(second).scrollTop, 80, 'a prompt block never moves by itself');
  // The first request finishes and the order changes: the list is rebuilt, and nothing jumps.
  outputOf(first).scrollTop = 250;
  await poll([entry(3, 'running', { output: long(800), prompt: long(600) }),
    entry(2, 'finished', { finish: 'stop', output: long(900) })]);
  assert.deepEqual([...view.children], [second, first], 'the rows were reordered, not rebuilt');
  assert.equal(outputOf(second).scrollTop, 120);
  assert.equal(promptOf(second).scrollTop, 80);
  assert.equal(outputOf(first).scrollTop, 250, 'a row that moved keeps its scroll');
  p.run('stopPeek()');
  // The cards' max (or lowest) is taken over the chart window, ignoring gaps.
  p = page();
  assert.equal(p.run('extremeOf([null, 3, NaN, 7, 1])'), 7);
  assert.equal(p.run('extremeOf([null, 3, NaN, 7, 1], true)'), 1);
  assert.equal(p.run('extremeOf([null, NaN])'), null);
  p.run('state = { history_len: 900, poll_s: 1 }');
  assert.equal(p.run('historyWindowLabel()'), '15 min');
  p.run('state = { history_len: 5400, poll_s: 1 }');
  assert.equal(p.run('historyWindowLabel()'), '1.5 h');
  console.log('monitor peek checks passed');
})().catch((error) => { console.error(error); process.exit(1); });
