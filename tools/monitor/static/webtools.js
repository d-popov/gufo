'use strict';

// The chat's browser tools. The model's tool calls run here, in the user's own browser:
// the inference server never fetches anything. A page can only read a response when the
// site allows it (CORS), so fetch_url asks the site directly first, without cookies. A
// site that does not allow it is read through the public reader at READER instead, which
// then sees that URL; every such step is labelled. web_search is a DuckDuckGo Lite page
// read the same way, because no search engine allows browser reads. Set READER to ''
// to use only sites that allow it (and no search).

window.GufoWebTools = (() => {
  const READER = 'https://r.jina.ai/';
  const SEARCH = 'https://lite.duckduckgo.com/lite/?q=';
  const MAX_BYTES = 2 * 1024 * 1024;
  const TIMEOUT_MS = 20000;
  const DEFAULT_CHARS = 8000;
  const SEARCH_CHARS = 6000;
  const MAX_CHARS = 30000;

  const TOOLS = [
    {
      type: 'function',
      function: {
        name: 'web_search',
        description: 'Search the web. Returns result titles, URLs and snippets; read a result with fetch_url.',
        parameters: {
          type: 'object',
          properties: { query: { type: 'string', description: 'The search query.' } },
          required: ['query'],
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'fetch_url',
        description: 'Fetch a public http(s) page or API URL and return its text. Use it to read pages found by web_search or public JSON APIs.',
        parameters: {
          type: 'object',
          properties: {
            url: { type: 'string', description: 'The full URL.' },
            max_chars: { type: 'integer', description: `Text to return, default ${DEFAULT_CHARS}, at most ${MAX_CHARS}.` },
          },
          required: ['url'],
        },
      },
    },
  ];

  class ToolError extends Error {}

  // Names a browser must not be aimed at: this machine, its network, IP addresses.
  function privateName(host) {
    const name = host.toLowerCase().replace(/^\[|\]$/g, '');
    return name === 'localhost' || /\.(localhost|local|internal|lan|home|corp)$/.test(name)
      || !name.includes('.') || /^[\d.]+$/.test(name) || name.includes(':');
  }

  function parseTarget(value) {
    let target;
    try { target = new URL(String(value ?? '').trim()); } catch { throw new ToolError('not a valid URL'); }
    if (target.protocol !== 'http:' && target.protocol !== 'https:') throw new ToolError('only http and https URLs');
    if (target.username || target.password) throw new ToolError('URLs with credentials are not fetched');
    if (privateName(target.hostname)) throw new ToolError('local and IP-address URLs are not fetched');
    return target;
  }

  // Runs work(signal) with the caller's abort and a time limit. A caller abort rethrows.
  async function withTimeout(signal, work) {
    const inner = new AbortController();
    const abort = () => inner.abort();
    if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(abort, TIMEOUT_MS);
    try {
      return await work(inner.signal);
    } catch (error) {
      if (signal?.aborted) throw error;
      if (inner.signal.aborted) throw new ToolError('timed out');
      throw error;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
    }
  }

  function get(url, signal, accept) {
    return fetch(url, {
      credentials: 'omit', referrerPolicy: 'no-referrer', cache: 'no-store', redirect: 'follow',
      headers: { Accept: accept }, signal,
    });
  }

  // The body as text, at most MAX_BYTES.
  async function readBody(response) {
    const reader = response.body?.getReader?.();
    if (!reader) return (await response.text()).slice(0, MAX_BYTES);
    const chunks = [];
    let total = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      total += value.length;
      if (total > MAX_BYTES) { reader.cancel().catch(() => {}); break; }
    }
    const bytes = new Uint8Array(Math.min(total, MAX_BYTES + 1));
    let offset = 0;
    for (const chunk of chunks) {
      const room = bytes.length - offset;
      if (room <= 0) break;
      bytes.set(chunk.subarray(0, room), offset);
      offset += Math.min(chunk.length, room);
    }
    return new TextDecoder().decode(bytes);
  }

  function readable(type) {
    return /^(text\/|application\/(json|[\w.+-]*\+json|xml|[\w.+-]*\+xml|javascript))/i.test(type)
      || type === '';
  }

  function htmlToText(html, base) {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    doc.querySelectorAll('script,style,noscript,template,svg,iframe').forEach((node) => node.remove());
    for (const link of doc.querySelectorAll('a[href]')) {
      try {
        const href = new URL(link.getAttribute('href'), base);
        if (/^https?:$/.test(href.protocol)) link.textContent = `${link.textContent.trim()} (${href.href})`;
      } catch { /* a link that does not parse stays as text */ }
    }
    const title = doc.title.trim();
    const text = (doc.body?.textContent ?? '').replace(/[ \t\r\f\v]+/g, ' ').replace(/\s*\n\s*/g, '\n')
      .replace(/\n{3,}/g, '\n\n').trim();
    return title ? `${title}\n\n${text}` : text;
  }

  function clip(text, maxChars) {
    if (text.length <= maxChars) return text;
    return `${text.slice(0, maxChars)}\n[truncated: first ${maxChars.toLocaleString()} of ${text.length.toLocaleString()} characters]`;
  }

  // The answer from the site itself, or null when the browser may not read it.
  async function direct(target, signal) {
    if (target.protocol !== 'https:') return null;  // an https page cannot read http
    return withTimeout(signal, async (inner) => {
      let response;
      try {
        response = await get(target.href, inner, 'application/json, text/plain;q=0.9, text/html;q=0.8, */*;q=0.1');
      } catch (error) {
        if (inner.aborted) throw error;
        return null;  // not allowed by the site (CORS), or no network
      }
      const type = (response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
      if (!readable(type)) throw new ToolError(`not text (${type})`);
      let body = await readBody(response);
      if (/html/.test(type)) body = htmlToText(body, response.url || target.href);
      const status = response.ok ? '' : `HTTP ${response.status} ${response.statusText || ''}`.trim() + '\n';
      return { text: status + body, via: 'direct' };
    });
  }

  async function reader(target, signal) {
    if (!READER) throw new ToolError('this site does not allow reading it from a browser, and the reader is off');
    return withTimeout(signal, async (inner) => {
      let response;
      try {
        response = await get(READER + target.href, inner, 'text/plain, */*;q=0.1');
      } catch (error) {
        if (inner.aborted) throw error;
        throw new ToolError('could not reach the reader (r.jina.ai)');
      }
      if (response.status === 429) throw new ToolError('the reader (r.jina.ai) rate limit is reached; try again in a minute');
      if (!response.ok) throw new ToolError(`the reader (r.jina.ai) failed (HTTP ${response.status})`);
      return { text: await readBody(response), via: 'reader' };
    });
  }

  // DuckDuckGo wraps result links in a redirect; the model needs the real addresses.
  function unwrapSearchLinks(text) {
    return text.replace(/https?:\/\/duckduckgo\.com\/l\/\?uddg=([^&)\s]+)[^)\s]*/g, (match, encoded) => {
      try { return decodeURIComponent(encoded); } catch { return match; }
    });
  }

  function clamp(value) {
    const number = Math.floor(Number(value));
    return Number.isFinite(number) && number > 0 ? Math.min(number, MAX_CHARS) : DEFAULT_CHARS;
  }

  async function fetchUrl(args, signal) {
    const target = parseTarget(args?.url);
    const maxChars = clamp(args?.max_chars);
    const found = (await direct(target, signal)) ?? (await reader(target, signal));
    return { text: clip(found.text, maxChars), via: found.via };
  }

  async function webSearch(args, signal) {
    const query = String(args?.query ?? '').trim();
    if (!query) throw new ToolError('the query is empty');
    const found = await reader(new URL(SEARCH + encodeURIComponent(query)), signal);
    return { text: clip(unwrapSearchLinks(found.text), SEARCH_CHARS), via: found.via };
  }

  // What the tool step says in the thread, before and after it has run.
  function label(name, args, done) {
    if (name === 'web_search') return `${done ? 'Searched' : 'Searching'} “${String(args?.query ?? '').slice(0, 80)}”`;
    let where = String(args?.url ?? '');
    try { const url = new URL(where); where = (url.host + url.pathname).replace(/\/$/, ''); } catch { /* shown as given */ }
    return `${done ? 'Read' : 'Reading'} ${where.slice(0, 90)}`;
  }

  // Runs one tool call. Failures come back as text for the model, never as exceptions;
  // only the caller's abort is rethrown.
  async function run(name, args, signal) {
    try {
      if (name !== 'web_search' && name !== 'fetch_url') throw new ToolError(`unknown tool ${name}`);
      const result = await (name === 'web_search' ? webSearch : fetchUrl)(args, signal);
      return { ok: true, text: result.text, via: result.via };
    } catch (error) {
      if (signal?.aborted) throw error;
      return { ok: false, text: `Error: ${error instanceof ToolError ? error.message : 'the request failed'}`, via: null };
    }
  }

  return { TOOLS, run, label, privateName, unwrapSearchLinks };
})();
