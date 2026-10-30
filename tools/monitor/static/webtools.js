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
  // Pictures and video come from Wikimedia Commons: freely licensed, readable from a browser
  // (CORS), and served from two hosts only, which is all the chat page's CSP lets an <img> or
  // <video> load. The model never supplies a media address; it only names what to look for.
  const COMMONS = 'https://commons.wikimedia.org/w/api.php';
  const MEDIA_HOSTS = ['upload.wikimedia.org', 'thumb.wikimedia.org'];
  const MEDIA_MAX = 8;

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
    {
      type: 'function',
      function: {
        name: 'find_media',
        description: 'Find freely licensed photos or short videos on Wikimedia Commons and show them to the user in the chat. '
          + 'You get their titles, licenses and page URLs but cannot see them. Use it when the user asks to see pictures or videos.',
        parameters: {
          type: 'object',
          properties: {
            query: { type: 'string', description: 'What to look for, in a few words.' },
            kind: { type: 'string', enum: ['image', 'video'], description: 'image (default) or video.' },
            count: { type: 'integer', description: `How many, default 6, at most ${MEDIA_MAX}.` },
          },
          required: ['query'],
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

  function https(value, hosts) {
    try {
      const url = new URL(String(value));
      return url.protocol === 'https:' && !url.username && !url.password && hosts.includes(url.hostname) ? url : null;
    } catch { return null; }
  }

  const plain = (value, max) => String(value ?? '').replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim().slice(0, max);

  // One media item as the chat may show it, or null. Used when a tool finds it and again when a
  // saved chat is drawn, so only addresses on the media hosts ever reach an <img> or <video>.
  function safeMedia(item) {
    const kind = item?.kind === 'video' ? 'video' : item?.kind === 'image' ? 'image' : null;
    const thumb = https(item?.thumb, MEDIA_HOSTS);
    const page = https(item?.page, ['commons.wikimedia.org']);
    if (!kind || !thumb || !page) return null;
    let src = thumb;
    if (kind === 'video') {
      src = https(item.src, ['upload.wikimedia.org']);
      if (!src) return null;
    }
    return {
      kind, title: plain(item.title, 120), src: src.href, thumb: thumb.href, page: page.href,
      license: plain(item.license, 60), author: plain(item.author, 80),
    };
  }

  const NAME = /^File:/i;
  const EXTENSION = /\.[a-z0-9]{2,5}$/i;

  async function findMedia(args, signal) {
    const query = String(args?.query ?? '').trim();
    if (!query) throw new ToolError('the query is empty');
    const kind = args?.kind === 'video' ? 'video' : 'image';
    const wanted = Number(args?.count);
    const count = Number.isFinite(wanted) && wanted >= 1 ? Math.min(Math.floor(wanted), MEDIA_MAX) : 6;
    const url = `${COMMONS}?${new URLSearchParams({
      action: 'query', format: 'json', origin: '*', generator: 'search', gsrnamespace: '6',
      gsrsearch: `${query} filetype:${kind === 'video' ? 'video' : 'bitmap'}`, gsrlimit: String(count * 2),
      prop: 'imageinfo', iiprop: 'url|mime|extmetadata', iiurlwidth: '480',
      iiextmetadatafilter: 'LicenseShortName|Artist',
    })}`;
    const body = await withTimeout(signal, async (inner) => {
      const response = await get(url, inner, 'application/json');
      if (!response.ok) throw new ToolError(`Wikimedia Commons answered HTTP ${response.status}`);
      try { return JSON.parse(await readBody(response)); } catch { throw new ToolError('Wikimedia Commons sent no usable answer'); }
    });
    const pages = Object.values(body?.query?.pages ?? {}).sort((a, b) => (a.index ?? 0) - (b.index ?? 0));
    const media = [];
    for (const page of pages) {
      const info = page?.imageinfo?.[0];
      const mime = String(info?.mime ?? '');
      // Video must be something a browser plays; a vector or a scan is not a photo.
      if (kind === 'video' ? !/^video\/(webm|mp4)$/.test(mime) : !/^image\/(jpeg|png|webp)$/.test(mime)) continue;
      const original = https(info?.url, ['upload.wikimedia.org']);
      if (original) original.search = '';
      const item = safeMedia({
        kind, title: String(page.title ?? '').replace(NAME, '').replace(EXTENSION, '').replace(/_/g, ' '),
        src: original?.href, thumb: info?.thumburl, page: info?.descriptionurl,
        license: info?.extmetadata?.LicenseShortName?.value, author: info?.extmetadata?.Artist?.value,
      });
      if (item) media.push(item);
      if (media.length >= count) break;
    }
    if (!media.length) return { text: `No ${kind === 'video' ? 'videos' : 'pictures'} found for “${query}” on Wikimedia Commons.`, via: 'direct', media };
    const lines = media.map((item, index) => {
      const credit = [item.license, item.author].filter(Boolean).join(', ');
      return `${index + 1}. ${item.title}${credit ? ` (${credit})` : ''} ${item.page}`;
    });
    return {
      text: `Shown to the user now, from Wikimedia Commons (they are freely licensed; credit the author when you mention one):\n${lines.join('\n')}`,
      via: 'direct', media,
    };
  }

  // What the tool step says in the thread, before and after it has run.
  function label(name, args, done) {
    if (name === 'find_media') {
      return `${done ? 'Found' : 'Finding'} ${args?.kind === 'video' ? 'videos' : 'pictures'} of “${String(args?.query ?? '').slice(0, 80)}”`;
    }
    if (name === 'web_search') return `${done ? 'Searched' : 'Searching'} “${String(args?.query ?? '').slice(0, 80)}”`;
    let where = String(args?.url ?? '');
    try { const url = new URL(where); where = (url.host + url.pathname).replace(/\/$/, ''); } catch { /* shown as given */ }
    return `${done ? 'Read' : 'Reading'} ${where.slice(0, 90)}`;
  }

  // Runs one tool call. Failures come back as text for the model, never as exceptions;
  // only the caller's abort is rethrown.
  async function run(name, args, signal) {
    try {
      const tool = { web_search: webSearch, fetch_url: fetchUrl, find_media: findMedia }[name];
      if (!tool) throw new ToolError(`unknown tool ${name}`);
      const result = await tool(args, signal);
      return { ok: true, text: result.text, via: result.via, ...(result.media?.length ? { media: result.media } : {}) };
    } catch (error) {
      if (signal?.aborted) throw error;
      return { ok: false, text: `Error: ${error instanceof ToolError ? error.message : 'the request failed'}`, via: null };
    }
  }

  return { TOOLS, run, label, privateName, unwrapSearchLinks, safeMedia };
})();
