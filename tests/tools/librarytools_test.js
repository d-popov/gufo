'use strict';

// Run with node tests/tools/librarytools_test.js. The chat's library tools send each call to the
// monitor's /api/library/call with the page's session, and hand the model the text that comes back.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('tools/monitor/static/librarytools.js', 'utf8');

function load(fetch) {
  const window = {};
  vm.runInNewContext(source, { window, fetch, JSON, String, Set, Promise });
  return window.GufoLibraryTools;
}
const answer = (status, body) => ({ ok: status < 400, status, json: async () => body });

(async () => {
  const sent = [];
  const tools = load(async (url, init) => { sent.push([url, init.method, init.credentials, JSON.parse(init.body)]); return answer(200, { text: 'found', is_error: false }); });
  assert.equal(JSON.stringify(tools.TOOLS.map((t) => t.function.name)), '["search_library","read_library"]');
  for (const tool of tools.TOOLS) assert.ok(tool.function.parameters.required.length, 'every tool names what it needs');

  assert.deepEqual(JSON.parse(JSON.stringify(await tools.run('search_library', { query: 'orbit' }))), { ok: true, text: 'found', via: null });
  assert.equal(JSON.stringify(sent), '[["/api/library/call","POST","same-origin",{"name":"search_library","arguments":{"query":"orbit"}}]]');

  // A mistake the library reports is the model's to read, not a crash; other failures say so briefly.
  const failing = load(async () => answer(200, { text: 'no page "x"', is_error: true }));
  assert.deepEqual(JSON.parse(JSON.stringify(await failing.run('read_library', { slug: 'x' }))), { ok: false, text: 'no page "x"', via: null });
  const refused = load(async () => answer(401, { error: 'unauthorized' }));
  assert.equal((await refused.run('read_library', { slug: 'x' })).text, 'Error: unauthorized');
  const down = load(async () => { throw new Error('offline'); });
  assert.equal((await down.run('read_library', { slug: 'x' })).text, 'Error: the library could not be reached');
  assert.equal((await tools.run('drop_tables', {})).ok, false);
  assert.equal(sent.length, 1, 'an unknown tool never reaches the monitor');

  // Only the caller's own abort is rethrown.
  const controller = new AbortController();
  const aborting = load(async () => { controller.abort(); throw new Error('aborted'); });
  await assert.rejects(aborting.run('search_library', { query: 'x' }, controller.signal));

  assert.equal(tools.label('search_library', { query: 'q' }, false), 'Searching the library for “q”');
  assert.equal(tools.label('read_library', { slug: 'a-b' }, true), 'Read library page a-b');

  console.log('Library tools: calls, failures and labels passed.');
})().catch((error) => { console.error(error); process.exitCode = 1; });
