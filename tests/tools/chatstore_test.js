'use strict';

// Run with node tests/tools/chatstore_test.js. The chats the first chat version kept in
// localStorage must survive the move to the browser database: the old copy is deleted only when
// every chat has been committed to storage that outlives the page.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('tools/monitor/static/chatstore.js', 'utf8');
const KEY = 'gufo-chat-histories-v1';
const legacy = [
  { id: 'a', title: 'First', updated: 1, messages: [{ role: 'user', content: 'hi' }] },
  { id: 'b', title: 'Second', updated: 2, messages: [] },
  { id: 'bad', messages: 'not a list' },
];

function load(globals = {}) {
  const window = { ...globals };
  vm.runInNewContext(source, { window, structuredClone, Promise });
  return window.GufoStore;
}

function storage(value) {
  const items = new Map(value === undefined ? [] : [[KEY, value]]);
  return { getItem: (key) => items.get(key) ?? null, removeItem: (key) => items.delete(key), has: (key) => items.has(key) };
}

(async () => {
  // No IndexedDB (a private window): the store works in memory, says it is not persistent, and
  // the old chats are shown but their only durable copy is left in place.
  const volatile = await load().open();
  assert.equal(volatile.persistent, false);
  const kept = storage(JSON.stringify(legacy));
  const shown = await load().migrate(volatile, kept, KEY, []);
  assert.equal(JSON.stringify(shown.map((chat) => chat.id)), '["a","b"]');
  assert.equal(kept.has(KEY), true, 'legacy copy survives a store that is lost with the page');

  // A store that commits: the chats move, once, and the old copy goes.
  const written = [];
  const durable = { persistent: true, put: async (table, chat) => { written.push(`${table}:${chat.id}`); } };
  const moved = storage(JSON.stringify(legacy));
  const ids = (await load().migrate(durable, moved, KEY, [{ id: 'a' }])).map((chat) => chat.id);
  assert.equal(JSON.stringify(ids), '["b"]', 'a chat the store already has is not written again');
  assert.equal(JSON.stringify(written), '["chats:b"]');
  assert.equal(moved.has(KEY), false);

  // A write the browser refuses (quota) keeps the old copy for the next visit.
  const full = { persistent: true, put: async (table, chat) => { if (chat.id === 'b') throw new Error('quota'); } };
  const refused = storage(JSON.stringify(legacy));
  assert.equal((await load().migrate(full, refused, KEY, [])).length, 0);
  assert.equal(refused.has(KEY), true);

  // Unreadable legacy data is left alone and does not stop the page.
  const garbage = storage('{not json');
  assert.equal((await load().migrate(durable, garbage, KEY, [])).length, 0);
  assert.equal(garbage.has(KEY), true);
  assert.equal((await load().migrate(durable, storage(), KEY, [])).length, 0);

  console.log('chatstore_test: ok');
})();
