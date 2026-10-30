'use strict';

// Run with node tests/tools/chatsync_test.js. Two devices of one account and a stand-in for the
// monitor's store: what each device ends up with after edits, deletes and conflicts.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('tools/monitor/static/chatsync.js', 'utf8');
const window = {};
vm.runInNewContext(source, { window, setTimeout, JSON, Object, Promise, Error });

// The monitor's side, with the rules of ChatStore: later `updated` wins, deletes leave tombstones.
function makeServer() {
  const items = new Map();
  let rev = 0;
  const wire = (item) => ({ ...item, data: item.deleted ? null : item.data });
  const server = {
    items, down: false, status: null, calls: [],
    async request(method, path, body) {
      server.calls.push(`${method} ${path.split('?')[0]}`);
      if (server.down) throw new TypeError('network error');
      if (server.status) return { status: server.status, json: { error: 'refused' } };
      const url = new URL(path, 'http://x');
      const [, , , what, kind, id] = url.pathname.split('/');
      if (what === 'sync') {
        const since = Number(url.searchParams.get('since'));
        const found = [...items.values()].filter((item) => item.rev > since).sort((a, b) => a.rev - b.rev);
        return { status: 200, json: { items: found.map(wire), more: false, through: rev } };
      }
      const key = `${kind}:${id}`;
      const old = items.get(key);
      if (method === 'PUT') {
        if (old && old.updated > body.updated) return { status: 200, json: { applied: false, item: wire(old) } };
        items.set(key, { kind, id, updated: body.updated, rev: ++rev, deleted: false, data: JSON.parse(JSON.stringify(body.data)) });
        return { status: 200, json: { applied: true, item: wire(items.get(key)) } };
      }
      const at = Number(url.searchParams.get('at'));
      if (old && old.updated > at && !old.deleted) return { status: 200, json: { applied: false, item: wire(old) } };
      items.set(key, { kind, id, updated: Math.max(at, old?.updated ?? 0), rev: ++rev, deleted: true, data: null });
      return { status: 200, json: { applied: true, item: wire(items.get(key)) } };
    },
  };
  return server;
}

// One device: its working copy, local storage and the engine.
function makeDevice(server, options = {}) {
  const local = { chat: new Map(), project: new Map() };
  const memory = new Map();
  const timers = [];
  const device = {
    local, timers, changes: [], errors: [], busy: new Set(),
    storage: { getItem: (k) => memory.get(k) ?? null, setItem: (k, v) => memory.set(k, v) },
  };
  device.sync = window.GufoSync.create({
    owner: 'admin', storage: device.storage, request: (...args) => server.request(...args),
    list: () => ['chat', 'project'].flatMap((kind) => [...local[kind].keys()].map((id) => ({ kind, id }))),
    read: (kind, id) => {
      const item = local[kind].get(id);
      return !item || (kind === 'chat' && item.messages.length === 0) ? null : { updated: item.updated, data: item };
    },
    stamp: (kind, id) => local[kind].get(id)?.updated ?? null,
    apply: (kind, id, data) => local[kind].set(id, JSON.parse(JSON.stringify(data))),
    drop: (kind, id) => local[kind].delete(id),
    busy: (kind, id) => device.busy.has(`${kind}:${id}`),
    now: options.now ?? (() => 1000),
    later: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    onChange: (changes) => device.changes.push(changes),
    onError: (message) => device.errors.push(message),
  });
  device.edit = (kind, id, fields) => {
    const item = { id, messages: [], ...local[kind].get(id), ...fields };
    local[kind].set(id, item);
    device.sync.touch(kind, id);
    return item;
  };
  return device;
}

(async () => {
  // A chat made on one device reaches the other, and a later edit there comes back.
  {
    const server = makeServer();
    const a = makeDevice(server), b = makeDevice(server);
    a.edit('chat', 'c1', { title: 'first', updated: 10, messages: [{ role: 'user', content: 'hi' }] });
    a.edit('project', 'p1', { name: 'Atlas', updated: 11 });
    await a.sync.sync();
    await b.sync.sync();
    assert.equal(b.local.chat.get('c1').title, 'first');
    assert.equal(b.local.project.get('p1').name, 'Atlas');
    assert.equal(JSON.stringify(b.changes[0].chats), '["c1"]');
    b.edit('chat', 'c1', { title: 'renamed on b', updated: 20 });
    await b.sync.sync();
    await a.sync.sync();
    assert.equal(a.local.chat.get('c1').title, 'renamed on b');
    assert.equal(a.local.chat.get('c1').messages.length, 1, 'the rest of the chat came along');
  }

  // Edits on both while apart: the later stamp wins everywhere, whichever device syncs first.
  for (const order of ['a-first', 'b-first']) {
    const server = makeServer();
    const a = makeDevice(server), b = makeDevice(server);
    a.edit('chat', 'c1', { title: 'base', updated: 10, messages: [{ role: 'user', content: 'x' }] });
    await a.sync.sync();
    await b.sync.sync();
    a.edit('chat', 'c1', { title: 'from a', updated: 30 });
    b.edit('chat', 'c1', { title: 'from b', updated: 40 });
    for (const device of order === 'a-first' ? [a, b, a] : [b, a, b]) await device.sync.sync();
    assert.equal(a.local.chat.get('c1').title, 'from b', order);
    assert.equal(b.local.chat.get('c1').title, 'from b', order);
  }

  // A delete reaches the other device, and an older edit cannot bring the chat back.
  {
    const server = makeServer();
    const a = makeDevice(server), b = makeDevice(server);
    a.edit('chat', 'c1', { title: 'doomed', updated: 10, messages: [{ role: 'user', content: 'x' }] });
    await a.sync.sync();
    await b.sync.sync();
    a.local.chat.delete('c1');
    a.sync.forget('chat', 'c1');
    await a.sync.sync();
    b.edit('chat', 'c1', { title: 'edited before the delete was seen', updated: 5 });
    await b.sync.sync();
    assert.equal(b.local.chat.has('c1'), false, 'b learned of the delete');
    assert.equal(server.items.get('chat:c1').deleted, true);
    b.edit('chat', 'c1', { title: 'written again later', updated: 5000, messages: [{ role: 'user', content: 'y' }] });
    await b.sync.sync();
    await a.sync.sync();
    assert.equal(a.local.chat.get('c1').title, 'written again later', 'a later edit revives it');
  }

  // The first sync of a browser with chats already in it sends them, but not empty chats.
  {
    const server = makeServer();
    const a = makeDevice(server);
    a.local.chat.set('old', { id: 'old', title: 'from before sync', updated: 7, messages: [{ role: 'user', content: 'x' }] });
    a.local.chat.set('blank', { id: 'blank', title: 'New chat', updated: 8, messages: [] });
    await a.sync.sync();
    assert.deepEqual([...server.items.keys()], ['chat:old']);
    const b = makeDevice(server);
    await b.sync.sync();
    assert.equal(b.local.chat.get('old').title, 'from before sync');
  }

  // A reply being written is not overwritten by what the monitor holds; it goes up afterwards.
  {
    const server = makeServer();
    const a = makeDevice(server), b = makeDevice(server);
    a.edit('chat', 'c1', { title: 'shared', updated: 10, messages: [{ role: 'user', content: 'x' }] });
    await a.sync.sync();
    await b.sync.sync();
    a.edit('chat', 'c1', { title: 'newer on a', updated: 50 });
    await a.sync.sync();
    b.busy.add('chat:c1');
    await b.sync.sync();
    assert.equal(b.local.chat.get('c1').title, 'shared', 'left alone while busy');
    b.busy.clear();
    await b.sync.sync();
    assert.equal(b.local.chat.get('c1').title, 'newer on a');
  }

  // A change is sent after a short delay, once however many edits came before it.
  {
    const server = makeServer();
    const a = makeDevice(server);
    for (let i = 0; i < 5; i++) a.edit('chat', 'c1', { title: `t${i}`, updated: 10 + i, messages: [{ role: 'user', content: 'x' }] });
    assert.deepEqual(a.timers.map((t) => t.ms), [1500]);
    await a.timers[0].fn();
    await new Promise((resolve) => setTimeout(resolve, 5));
    assert.equal(server.items.get('chat:c1').data.title, 't4');
    assert.equal(server.calls.filter((c) => c.startsWith('PUT')).length, 1);
  }

  // Offline keeps what is pending and sends it when the monitor is back.
  {
    const server = makeServer();
    const a = makeDevice(server);
    a.edit('chat', 'c1', { title: 'written offline', updated: 10, messages: [{ role: 'user', content: 'x' }] });
    server.down = true;
    await assert.rejects(a.sync.sync(), /network/);
    assert.equal(a.sync.pending, 1);
    server.down = false;
    await a.sync.sync();
    assert.equal(a.sync.pending, 0);
    assert.equal(server.items.get('chat:c1').data.title, 'written offline');
  }

  // A monitor without sync turns it off; a refused item is reported once and not retried forever.
  {
    const server = makeServer();
    server.status = 404;
    const a = makeDevice(server);
    a.edit('chat', 'c1', { title: 'x', updated: 1, messages: [{ role: 'user', content: 'x' }] });
    await assert.rejects(a.sync.sync(), /off/);
    assert.equal(a.sync.enabled, false);
    const scheduled = a.timers.length;
    a.edit('chat', 'c2', { title: 'y', updated: 1, messages: [{ role: 'user', content: 'x' }] });
    assert.equal(a.timers.length, scheduled, 'nothing new is scheduled once it is off');

    const full = makeServer();
    full.status = 413;
    const b = makeDevice(full);
    b.edit('chat', 'c1', { title: 'big', updated: 1, messages: [{ role: 'user', content: 'x' }] });
    await b.sync.sync().catch(() => {});
    assert.equal(b.errors.length, 1);
    assert.equal(b.sync.pending, 0);
  }

  // What was pending survives a reload of the page.
  {
    const server = makeServer();
    const a = makeDevice(server);
    a.edit('chat', 'c1', { title: 'pending', updated: 10, messages: [{ role: 'user', content: 'x' }] });
    const again = window.GufoSync.create({
      owner: 'admin', storage: a.storage, request: (...args) => server.request(...args),
      list: () => [], read: (kind, id) => ({ updated: 10, data: a.local[kind].get(id) }), stamp: () => null,
      apply: () => {}, drop: () => {}, later: () => 1,
    });
    assert.equal(again.pending, 1);
    await again.sync();
    assert.equal(server.items.get('chat:c1').data.title, 'pending');
  }

  console.log('chatsync_test: ok');
})().catch((error) => { console.error(error); process.exitCode = 1; });
