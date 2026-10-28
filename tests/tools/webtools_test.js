'use strict';

// Run with node tests/tools/webtools_test.js. The chat's browser tools, with a fake fetch:
// which requests they make, in what order, and what the model is told.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('tools/monitor/static/webtools.js', 'utf8');

function load(handler) {
  const calls = [];
  const fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return handler(String(url), options);
  };
  const window = {};
  vm.runInNewContext(source, {
    window, fetch, URL, AbortController, setTimeout, clearTimeout, TextDecoder, Uint8Array,
  });
  return { tools: window.GufoWebTools, calls };
}

function reply(body, { status = 200, type = 'application/json', url = '' } = {}) {
  return {
    ok: status >= 200 && status < 300, status, statusText: status === 404 ? 'Not Found' : '', url,
    headers: { get: (name) => (name.toLowerCase() === 'content-type' ? type : null) },
    text: async () => body,
  };
}

(async () => {
  // Targets a browser must not be aimed at never produce a request at all.
  {
    const { tools, calls } = load(() => reply('x'));
    for (const url of ['http://localhost/x', 'https://127.0.0.1/x', 'https://192.168.1.1/', 'https://[::1]/',
      'https://intranet/x', 'https://printer.local/', 'https://user:pw@example.com/', 'ftp://example.com/',
      'file:///etc/passwd', 'javascript:alert(1)', 'not a url', '']) {
      const result = await tools.run('fetch_url', { url }, undefined);
      assert.equal(result.ok, false, url);
      assert.match(result.text, /^Error: /, url);
    }
    assert.equal(calls.length, 0, 'nothing was requested');
  }

  // A site that allows browser reads is asked directly, without cookies, and nobody else.
  {
    const { tools, calls } = load(() => reply('{"a":1}'));
    const result = await tools.run('fetch_url', { url: 'https://api.example.com/v1?q=1' });
    assert.deepEqual([result.ok, result.via, result.text], [true, 'direct', '{"a":1}']);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, 'https://api.example.com/v1?q=1');
    assert.equal(calls[0].options.credentials, 'omit');
    assert.equal(calls[0].options.referrerPolicy, 'no-referrer');
  }

  // A site that does not (the browser's fetch throws) goes through the reader, once.
  {
    const { tools, calls } = load((url) => {
      if (url.startsWith('https://r.jina.ai/')) return reply('# Example', { type: 'text/plain' });
      throw new TypeError('Failed to fetch');
    });
    const result = await tools.run('fetch_url', { url: 'https://example.com/page' });
    assert.deepEqual([result.ok, result.via, result.text], [true, 'reader', '# Example']);
    assert.deepEqual(calls.map((call) => call.url),
      ['https://example.com/page', 'https://r.jina.ai/https://example.com/page']);
  }

  // An http page cannot be read by an https page: straight to the reader.
  {
    const { tools, calls } = load(() => reply('plain', { type: 'text/plain' }));
    const result = await tools.run('fetch_url', { url: 'http://example.com/' });
    assert.equal(result.via, 'reader');
    assert.deepEqual(calls.map((call) => call.url), ['https://r.jina.ai/http://example.com/']);
  }

  // Reader failures say what happened.
  {
    const limited = load((url) => (url.startsWith('https://r.jina.ai/') ? reply('', { status: 429 }) : (() => { throw new TypeError('x'); })()));
    assert.match((await limited.tools.run('fetch_url', { url: 'https://example.com/' })).text, /rate limit/);
    const broken = load((url) => (url.startsWith('https://r.jina.ai/') ? reply('', { status: 500 }) : (() => { throw new TypeError('x'); })()));
    assert.match((await broken.tools.run('fetch_url', { url: 'https://example.com/' })).text, /HTTP 500/);
  }

  // Binary content is refused, and an HTTP error from an allowed site is reported with its status.
  {
    const image = load(() => reply('PNG', { type: 'image/png' }));
    const refused = await image.tools.run('fetch_url', { url: 'https://example.com/a.png' });
    assert.match(refused.text, /not text \(image\/png\)/);
    assert.equal(image.calls.length, 1, 'the reader is not asked for a binary file');
    const missing = load(() => reply('{"error":"no such page"}', { status: 404 }));
    const result = await missing.tools.run('fetch_url', { url: 'https://api.example.com/x' });
    assert.equal(result.ok, true);
    assert.match(result.text, /^HTTP 404 Not Found\n\{"error"/);
  }

  // Long answers are cut at max_chars, which is clamped; a bad value falls back to the default.
  {
    const long = 'x'.repeat(50000);
    const { tools } = load(() => reply(long, { type: 'text/plain' }));
    const length = async (max) => (await tools.run('fetch_url', { url: 'https://api.example.com/', max_chars: max })).text.split('\n')[0].length;
    assert.equal(await length(100), 100);
    assert.equal(await length(1e9), 30000);
    assert.equal(await length('nonsense'), 8000);
    assert.equal(await length(-5), 8000);
    const cut = (await tools.run('fetch_url', { url: 'https://api.example.com/', max_chars: 100 })).text;
    assert.match(cut, /\[truncated: first 100 of 50,000 characters\]$/);
  }

  // Search reads one DuckDuckGo Lite page through the reader and hands back real addresses.
  {
    const page = '1.[Qwen](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2FQwenLM%2FQwen3.8&rut=abc123) text\n'
      + '2.[Other](https://example.org/direct) text';
    const { tools, calls } = load(() => reply(page, { type: 'text/plain' }));
    const result = await tools.run('web_search', { query: 'qwen 3.8 & more' });
    assert.equal(result.via, 'reader');
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, 'https://r.jina.ai/https://lite.duckduckgo.com/lite/?q=qwen%203.8%20%26%20more');
    assert.match(result.text, /\[Qwen\]\(https:\/\/github\.com\/QwenLM\/Qwen3\.8\) text/);
    assert.doesNotMatch(result.text, /duckduckgo\.com\/l\//);
    assert.match(result.text, /example\.org\/direct/);
    assert.match((await tools.run('web_search', { query: '   ' })).text, /query is empty/);
  }

  // Unknown tools and malformed arguments are answered, not thrown.
  {
    const { tools } = load(() => reply('x'));
    assert.match((await tools.run('rm_rf', {})).text, /unknown tool/);
    assert.equal((await tools.run('fetch_url', undefined)).ok, false);
    assert.equal((await tools.run('fetch_url', { url: 42 })).ok, false);
  }

  // The user's Stop reaches the request and is rethrown, not turned into a tool error.
  {
    const stopper = new AbortController();
    const { tools } = load((url, options) => new Promise((resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
    }));
    const pending = tools.run('fetch_url', { url: 'https://api.example.com/slow' }, stopper.signal);
    setTimeout(() => stopper.abort(), 10);
    await assert.rejects(pending, { name: 'AbortError' });
  }

  // What the thread shows while a step runs and after it.
  {
    const { tools } = load(() => reply('x'));
    assert.equal(tools.label('web_search', { query: 'cats' }, false), 'Searching “cats”');
    assert.equal(tools.label('fetch_url', { url: 'https://example.com/a/b/' }, true), 'Read example.com/a/b');
    assert.equal(tools.TOOLS.map((tool) => tool.function.name).join(), 'web_search,fetch_url');
  }
  console.log('Browser tools: blocked targets, direct and reader paths, errors, limits, search links and Stop passed.');
})().catch((error) => { console.error(error); process.exitCode = 1; });
