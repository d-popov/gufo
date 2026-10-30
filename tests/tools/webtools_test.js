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
    window, fetch, URL, URLSearchParams, AbortController, setTimeout, clearTimeout, TextDecoder, Uint8Array,
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
    assert.equal(tools.TOOLS.map((tool) => tool.function.name).join(), 'web_search,fetch_url,find_media');
    assert.equal(tools.label('find_media', { query: 'horses' }, false), 'Finding pictures of “horses”');
    assert.equal(tools.label('find_media', { query: 'horses', kind: 'video' }, true), 'Found videos of “horses”');
  }
  // find_media asks Wikimedia Commons only, and shows nothing but Commons files: the model names
  // what to look for and never supplies an address.
  {
    const page = (index, title, mime, extra = {}) => ({
      index, title, imageinfo: [{
        mime, url: `https://upload.wikimedia.org/wikipedia/commons/a/a2/${index}.bin?utm_source=x`,
        thumburl: `https://thumb.wikimedia.org/wikipedia/commons/thumb/a/a2/${index}/480px-${index}.jpg`,
        descriptionurl: `https://commons.wikimedia.org/wiki/${title.replace(' ', '_')}`,
        extmetadata: { LicenseShortName: { value: 'CC BY-SA 4.0' }, Artist: { value: '<a href="//x">Jane</a> Doe' } },
        ...extra,
      }],
    });
    const pages = {
      3: page(3, 'File:Third horse.jpg', 'image/jpeg'),
      1: page(1, 'File:First_horse.JPG', 'image/jpeg'),
      2: page(2, 'File:Drawing.svg', 'image/svg+xml'),
      4: page(4, 'File:Tracker.jpg', 'image/jpeg', { thumburl: 'https://evil.example/pixel.jpg?d=secret' }),
      5: page(5, 'File:Clip.webm', 'video/webm'),
    };
    const answer = () => reply(JSON.stringify({ query: { pages } }));
    const { tools, calls } = load(answer);
    const found = await tools.run('find_media', { query: 'horse', count: 99 });
    assert.equal(found.ok, true);
    assert.equal(calls.length, 1);
    const request = new URL(calls[0].url);
    assert.equal(request.origin + request.pathname, 'https://commons.wikimedia.org/w/api.php');
    assert.equal(request.searchParams.get('gsrsearch'), 'horse filetype:bitmap');
    assert.equal(request.searchParams.get('gsrlimit'), '16', 'count is capped at 8, asked twice over to survive filtering');
    assert.equal(calls[0].options.credentials, 'omit');
    assert.equal(calls[0].options.referrerPolicy, 'no-referrer');
    // Result order, no vector drawings, no tracker host, no videos among pictures.
    assert.equal(found.media.map((item) => item.title).join('|'), 'First horse|Third horse');
    assert.equal(found.media[0].author, 'Jane Doe');
    assert.equal(found.media[0].src, found.media[0].thumb, 'a picture is shown by its thumbnail');
    assert.match(found.text, /1\. First horse \(CC BY-SA 4\.0, Jane Doe\) https:\/\/commons\.wikimedia\.org\/wiki\/File:First_horse\.JPG/);
    assert.doesNotMatch(found.text, /evil\.example|Tracker/);

    const clips = await load(answer).tools.run('find_media', { query: 'horse', kind: 'video' });
    assert.equal(clips.media.map((item) => `${item.kind}:${item.title}`).join('|'), 'video:Clip');
    assert.equal(clips.media[0].src, 'https://upload.wikimedia.org/wikipedia/commons/a/a2/5.bin', 'the file, without tracking parameters');

    const none = await load(() => reply('{"batchcomplete":""}')).tools.run('find_media', { query: 'zzzz' });
    assert.equal(none.ok, true);
    assert.equal(none.media, undefined);
    assert.match(none.text, /No pictures found/);
    assert.equal((await load(() => reply('x', { status: 429 })).tools.run('find_media', { query: 'a' })).ok, false);
    assert.equal((await tools.run('find_media', { query: '  ' })).ok, false);

    // Whatever a saved chat holds, only Commons media on the two media hosts is drawn.
    const safe = tools.safeMedia;
    const good = {
      kind: 'image', title: 't', thumb: 'https://thumb.wikimedia.org/a.jpg', src: 'https://thumb.wikimedia.org/a.jpg',
      page: 'https://commons.wikimedia.org/wiki/File:A.jpg',
    };
    assert.ok(safe(good));
    for (const bad of [
      { ...good, thumb: 'https://evil.example/a.jpg' }, { ...good, thumb: 'http://thumb.wikimedia.org/a.jpg' },
      { ...good, thumb: 'https://thumb.wikimedia.org.evil.example/a.jpg' }, { ...good, page: 'https://evil.example/wiki' },
      { ...good, thumb: 'https://u:p@thumb.wikimedia.org/a.jpg' }, { ...good, kind: 'audio' },
      { ...good, kind: 'video', src: 'https://evil.example/v.webm' }, null, 'x',
    ]) assert.equal(safe(bad), null, JSON.stringify(bad));
  }
  console.log('Browser tools: blocked targets, direct and reader paths, errors, limits, search links and Stop passed.');
})().catch((error) => { console.error(error); process.exitCode = 1; });
