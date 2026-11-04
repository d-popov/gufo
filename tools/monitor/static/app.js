'use strict';

// Cards: one per live metric, each with a sparkline over the history window.
const CARDS = [
  { key: 'decode_tps', icon: 'zap', label: 'Decode (all sessions)', unit: 'tok/s', digits: 1 },
  { key: 'prefill_tps', icon: 'layers', label: 'Prefill (all sessions)', unit: 'tok/s', digits: 0 },
  { key: 'running', icon: 'activity', label: 'Requests running', digits: 0, sub: (s) => `${fmt(s.queued, 0)} queued` },
  { key: 'cache_reuse_pct', icon: 'database', label: 'Cache reuse', unit: '%', digits: 1, max: 100,
    value: (s) => s.cache_reuse_window_pct, sub: () => 'completed requests, last 5 min' },
  { key: 'accept_pct', icon: 'target', label: 'Draft acceptance', unit: '%', digits: 1, max: 100,
    value: (s) => s.accept_window_pct, sub: () => 'completed requests, last 5 min' },
  { key: 'gpu_busy_pct', icon: 'cpu', label: 'GPU busy', unit: '%', digits: 0, max: 100,
    sub: (s) => (s.gpu_mhz != null ? `${fmt(s.gpu_mhz, 0)} MHz` : '') },
  { key: 'gtt_used_gb', icon: 'memory-stick', label: 'GPU memory (GTT)', unit: 'GB', digits: 1,
    sub: (s) => `of ${fmt(s.gtt_total_gb, 1)} GB` },
  { key: 'mem_avail_gb', icon: 'memory-stick', label: 'Memory available', unit: 'GB', digits: 1,
    sub: (s) => `swap ${fmt(s.swap_used_gb, 1)} of ${fmt(s.swap_total_gb, 1)} GB used` },
  { key: 'gpu_temp_c', icon: 'thermometer', label: 'GPU temperature', unit: '°C', digits: 0 },
  { key: 'gpu_power_w', icon: 'plug-zap', label: 'GPU power', unit: 'W', digits: 0 },
  { key: 'cpu_pct', icon: 'cpu', label: 'CPU', unit: '%', digits: 0, max: 100 },
];

// The long-window tile, last in the grid: only the headline rows of [label, 24 h, 7 days]
// from GET /api/stats/long, refreshed every LONG_REFRESH_MS. The full set (GPU, errors,
// cache reuse, coverage) is the portal's Usage tab. Revenue rows need --billing-config.
const LONG_ROWS = [
  ['Prefill tok/s', (w) => fmt(w.prefill_tps, 0)],
  ['Tokens/h', (w) => fmtCompact(w.tokens_per_hour)],
  ['Revenue/h', (w, r) => fmtUsdShort(r == null ? null : r.total / w.window_hours)],
  ['Revenue/day', (w, r) => fmtUsdShort(r == null ? null : (r.total * 24) / w.window_hours)],
  ['Image enc ms/req', (w) => fmt(w.encode_ms_per_request, 0)],
];
const LONG_REFRESH_MS = 60000;
const longTile = {};  // the tile's nodes

// Recent-request columns; optional ones appear only when some row has them.
const REQUEST_COLUMNS = [
  { title: 'Time', cell: (r) => fmtTime(r.t) },
  { title: 'Model', key: 'model', optional: true, cell: (r) => r.model ?? '' },
  { title: 'Client', key: 'client', optional: true, cell: (r) => r.client ?? '' },
  { title: 'Key', key: 'key', optional: true, cell: (r) => r.key ?? '' },
  { title: 'Path', cell: (r) => r.path ?? '' },
  { title: 'Result', cell: (r) => [r.status, r.outcome !== 'completed' ? r.outcome : null, r.finish]
      .filter((x) => x != null && x !== '').join(' · '),
    bad: (r) => (r.status ?? 0) >= 400 },
  { title: 'Prompt', num: true, cell: (r) => fmt(r.prompt_tokens, 0) },
  { title: 'Cached', num: true, cell: (r) => fmt(r.cached_tokens, 0) },
  { title: 'Generated', num: true, cell: (r) => fmt(r.generated_tokens, 0) },
  { title: 'Images', key: 'images', optional: true, cell: imageCell, tip: imageTip },
  { title: 'TTFT ms', num: true, cell: (r) => fmt(r.ttft_ms, 0) },
  { title: 'Prefill tok/s', num: true, cell: (r) => fmt(r.prefill_tps, 1) },
  { title: 'Decode tok/s', num: true, cell: (r) => fmt(r.decode_tps, 1) },
  { title: 'Batch', num: true, cell: (r) => fmt(r.batch_width, 0) },
  { title: 'Accept %', num: true, cell: (r) => fmt(r.acceptance_pct, 1) },
  { title: 'Duration s', num: true, cell: (r) => fmt(r.duration_ms != null ? r.duration_ms / 1000 : null, 1) },
];

const RECENT_ROWS = 20;
const SVG = 'http://www.w3.org/2000/svg';

let state = null;
let requests = [];
let requestsDirty = true;
let renderQueued = false;
let streamUp = false;
let source = null;
let expired = false;  // the admin session ended: no more data until a new sign-in
let longStats = null;  // GET /api/stats/long, or null before it answers or without it

// ---------------------------------------------------------------- formatting (browser locale)

const numberFormats = new Map();

function fmt(value, digits) {
  if (value == null || !Number.isFinite(value)) return '—';
  if (!numberFormats.has(digits)) {
    numberFormats.set(digits, new Intl.NumberFormat(undefined, {
      minimumFractionDigits: digits, maximumFractionDigits: digits }));
  }
  return numberFormats.get(digits).format(value);
}

const compactFormat = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });

// Micro-dollars as whole cents; null without billing.
function fmtUsdShort(micro) {
  return micro == null || !Number.isFinite(micro) ? '—' : `$${fmt(micro / 1e6, 2)}`;
}

function fmtCompact(value) {
  return value == null || !Number.isFinite(value) ? '—' : compactFormat.format(value);
}

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: 'short' });
const clockFormat = new Intl.DateTimeFormat(undefined, { timeStyle: 'medium' });

function fmtTime(t) {
  if (t == null) return '';
  const date = new Date(t * 1000);
  const day = dateFormat.format(date);
  const clock = clockFormat.format(date);
  return day === dateFormat.format(new Date()) ? clock : `${day} ${clock}`;
}

function joinKnown(parts) {
  const known = parts.filter((part) => part != null);
  return known.length ? known.join(' · ') : '—';
}

// Recent-requests Images cell and its tooltip: empty without image fields (text requests,
// Gufo builds before them); a field Gufo did not report is left out, never shown as 0.
function imageCell(r) {
  if (r.images == null) return '';
  return [`${fmt(r.images, 0)} img`,
    r.image_tokens != null ? `${fmtCompact(r.image_tokens)} tok` : null,
    r.encode_ms != null ? `${fmt(r.encode_ms, 0)} ms enc` : null].filter(Boolean).join(' · ');
}

function imageTip(r) {
  if (r.images == null) return '';
  const lines = [];
  const input = [r.image_bytes != null ? `${fmt(r.image_bytes / 1e6, 2)} MB received` : null,
    r.image_px != null ? `${fmt(r.image_px / 1e6, 2)} MP after resize` : null].filter(Boolean);
  lines.push(`${fmt(r.images, 0)} image(s)${input.length ? `: ${input.join(', ')}` : ''}`);
  if (r.image_px_hits != null || r.image_px_misses != null) {
    lines.push(`Pixel cache (decode/resize only): ${fmt(r.image_px_hits ?? 0, 0)} hit, ` +
      `${fmt(r.image_px_misses ?? 0, 0)} miss`);
  }
  if (r.encoder_images != null || r.encode_ms != null) {
    lines.push(`Vision encoder: ${r.encoder_images != null ? `${fmt(r.encoder_images, 0)} image(s)` : 'ran'}` +
      `${r.encode_ms != null ? `, ${fmt(r.encode_ms, 1)} ms` : ''}`);
  }
  if (r.image_tokens != null) lines.push(`Visual tokens in prompt: ${fmt(r.image_tokens, 0)}`);
  if (r.image_tokens_reused != null) {
    lines.push(`Visual tokens reused (continuation/KV, not prefilled): ${fmt(r.image_tokens_reused, 0)}`);
  }
  return lines.join('\n');
}

// ---------------------------------------------------------------- DOM helpers

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (name === 'class') node.className = value;
    else node.setAttribute(name, value);
  }
  for (const child of children) node.append(child);
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
  return node;
}

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- cards

function buildCards() {
  const root = $('cards');
  root.replaceChildren();
  for (const card of CARDS) {
    const value = el('span', { class: 'card-number' }, '—');
    const sub = el('div', { class: 'card-sub' });
    const area = svg('path', { class: 'area' });
    const line = svg('path');
    const dots = svg('path', { class: 'dots' });
    const chart = svg('svg', { class: 'spark', viewBox: '0 0 100 30', preserveAspectRatio: 'none',
                               'aria-hidden': 'true' });
    chart.append(area, line, dots);
    const unit = el('span', { class: 'card-unit' });
    root.append(el('div', { class: 'card' },
      el('div', { class: 'card-label with-icon' }, window.GufoIcons.svg(card.icon), card.label),
      el('div', { class: 'card-value' }, value, unit),
      sub, chart));
    Object.assign(card, { valueNode: value, unitNode: unit, subNode: sub, area, line, dots });
  }
  root.append(buildLongCard());
}

function buildLongCard() {
  longTile.value = el('span', { class: 'card-number' }, '—');
  longTile.unit = el('span', { class: 'card-unit' });
  longTile.week = el('span', { class: 'card-alt' });
  longTile.cells = LONG_ROWS.map(() => [el('td', { class: 'num' }, '—'), el('td', { class: 'num' }, '—')]);
  const head = el('tr', {}, el('th', {}, ''), el('th', { class: 'num' }, '24 h'), el('th', { class: 'num' }, '7 d'));
  const rows = LONG_ROWS.map(([label], i) => el('tr', {}, el('th', {}, label), ...longTile.cells[i]));
  return el('div', { class: 'card card-long', title: 'Decode tok/s: generated tokens over the time spent decoding them\n' +
      'Image enc ms/req: vision encoder time per request with images (— before the server logged it)' },
    el('div', { class: 'card-label with-icon' }, window.GufoIcons.svg('calendar-clock'), 'Last 24 h / 7 days'),
    el('div', { class: 'card-value' }, longTile.value, longTile.unit, longTile.week),
    el('table', { class: 'long-table' }, el('thead', {}, head), el('tbody', {}, ...rows)),
    el('a', { class: 'card-sub', href: '/' }, 'More in Usage'));
}

function renderLong() {
  const day = longStats?.windows?.['24h'];
  const week = longStats?.windows?.['168h'];
  longTile.value.textContent = fmt(day?.decode_tps, 1);
  longTile.unit.textContent = day?.decode_tps == null ? '' : 'tok/s decode';
  longTile.week.textContent = `7 d ${fmt(week?.decode_tps, 1)}`;
  LONG_ROWS.forEach(([, cell], i) => {
    const revenue = longStats?.revenue_micro;
    longTile.cells[i][0].textContent = day ? cell(day, revenue?.['24h']) : '—';
    longTile.cells[i][1].textContent = week ? cell(week, revenue?.['168h']) : '—';
  });
}

async function loadLongStats() {
  if (expired) return;
  const response = await fetch('/api/stats/long', { credentials: 'same-origin', cache: 'no-store' });
  if (response.status === 401) {
    sessionExpired();
    return;
  }
  longStats = response.ok ? await response.json() : null;
  renderLong();
}

// Sparkline over the full history window: gaps (null) break the line, lone
// points (e.g. a request completing) draw as dots.
function sparkline(card, values) {
  const n = state.history_len;
  const present = values.filter((v) => v != null);
  const top = Math.max(card.max ?? 0, ...present, 1e-9);
  const x = (i) => (100 * (n - values.length + i)) / Math.max(n - 1, 1);
  const y = (v) => 29 - (27 * v) / top;
  let line = '';
  let area = '';
  let dots = '';
  let run = [];
  const flush = () => {
    if (run.length === 1) {
      const [i, v] = run[0];
      dots += `M${x(i) - 0.6},${y(v)}h1.2v1.2h-1.2z`;
    } else if (run.length > 1) {
      const points = run.map(([i, v]) => `${x(i).toFixed(2)},${y(v).toFixed(2)}`);
      line += `M${points.join('L')}`;
      area += `M${x(run[0][0]).toFixed(2)},29L${points.join('L')}L${x(run[run.length - 1][0]).toFixed(2)},29Z`;
    }
    run = [];
  };
  values.forEach((v, i) => (v == null ? flush() : run.push([i, v])));
  flush();
  card.line.setAttribute('d', line);
  card.area.setAttribute('d', area);
  card.dots.setAttribute('d', dots);
}

function renderCards() {
  const sample = state.sample ?? {};
  for (const card of CARDS) {
    const value = card.value ? card.value(sample) : sample[card.key];
    card.valueNode.textContent = fmt(value, card.digits);
    card.unitNode.textContent = value == null ? '' : card.unit ?? '';
    card.subNode.textContent = card.sub ? card.sub(sample) : '';
    sparkline(card, state.history[card.key] ?? []);
  }
}

// ---------------------------------------------------------------- pill and model

function renderPill() {
  const pill = $('pill');
  const status = state.status ?? {};
  const slots = state.slots ?? [];
  const sample = state.sample ?? {};
  let text;
  let kind;
  if (expired) {
    [text, kind] = ['Signed out', 'down'];
  } else if (!streamUp) {
    [text, kind] = ['Monitor offline', 'down'];
  } else if (status.state === 'down') {
    [text, kind] = ['Down', 'down'];
  } else if (status.state === 'loading') {
    [text, kind] = ['Loading', 'loading'];
  } else {
    const busy = slots.filter((s) => s.processing);
    const generating = busy.filter((s) => s.decoded_tokens > 0 || s.decode_tps > 0);
    if (generating.length) {
      const rate = sample.decode_tps == null ? '' : ` · ${fmt(sample.decode_tps, 1)} tok/s`;
      [text, kind] = [`Generating${rate}`, 'generating'];
    } else if (busy.length) {
      [text, kind] = [`Reading prompt · ${fmt(sample.prefill_tps, 0)} tok/s`, 'prompt'];
    } else if ((sample.queued ?? 0) > 0) {
      [text, kind] = ['Queued', 'queued'];
    } else {
      [text, kind] = ['Waiting', 'waiting'];
    }
  }
  pill.textContent = text;
  pill.className = `pill pill-${kind}`;

  const detail = $('detail');
  const message = expired ? '' : !streamUp ? 'Lost the connection to the monitor; reconnecting.'
    : status.state !== 'ready' && status.detail ? status.detail : '';
  detail.textContent = message;
  detail.hidden = !message;

  const models = (state.models ?? []).map((m) => m.id);
  $('model').textContent = models.length ? models.join(' · ') : status.model ?? '';
}

// ---------------------------------------------------------------- sessions

function renderSessions() {
  const body = $('sessions').tBodies[0];
  const groups = new Map();
  for (const slot of state.slots ?? []) {
    const key = slot.model ?? '';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(slot);
  }
  const rows = [];
  if (!groups.size) {
    rows.push(el('tr', {}, el('td', { colspan: '7', class: 'muted' },
      state.status?.state === 'ready' ? 'No sessions reported.' : 'The inference server is not ready.')));
  }
  const multiple = groups.size > 1 || (groups.size === 1 && !groups.has(''));
  for (const [model, slots] of groups) {
    if (multiple) {
      const ctx = slots[0].n_ctx;
      rows.push(el('tr', { class: 'group' }, el('td', { colspan: '7' },
        `${model} · ${slots.length} session${slots.length === 1 ? '' : 's'} × ${fmt(ctx, 0)} context`)));
    }
    for (const slot of slots) {
      const used = slot.prompt_tokens + slot.decoded_tokens;
      const share = slot.n_ctx ? Math.min(1, used / slot.n_ctx) : 0;
      let stateText = 'Idle';
      let stateClass = 'muted';
      let rate = '';
      if (slot.processing && (slot.decoded_tokens > 0 || slot.decode_tps > 0)) {
        [stateText, stateClass, rate] = ['Generating', 'state-busy', fmt(slot.decode_tps, 1)];
      } else if (slot.processing) {
        [stateText, stateClass, rate] = ['Reading prompt', 'state-prompt', fmt(slot.prefill_tps, 0)];
      }
      const bar = el('span');
      bar.style.width = `${(share * 100).toFixed(1)}%`;
      if (share > 0.85) bar.className = 'high';
      const cachedShare = slot.prompt_tokens ? (100 * slot.cached_tokens) / slot.prompt_tokens : null;
      rows.push(el('tr', {},
        el('td', {}, `#${slot.id}`),
        el('td', { class: stateClass }, stateText),
        el('td', {}, el('div', { class: 'fill' }, el('div', { class: 'bar' }, bar),
          el('span', { class: 'muted' }, `${fmt(used, 0)} / ${fmt(slot.n_ctx, 0)}`))),
        el('td', { class: 'num' }, fmt(slot.prompt_tokens, 0)),
        el('td', { class: 'num' }, cachedShare == null ? '—' : `${fmt(cachedShare, 0)} %`),
        el('td', { class: 'num' }, fmt(slot.decoded_tokens, 0)),
        el('td', { class: 'num' }, rate)));
    }
  }
  body.replaceChildren(...rows);
}

// ---------------------------------------------------------------- requests

function renderRequests() {
  if (!requestsDirty) return;
  requestsDirty = false;
  const table = $('requests');
  const columns = REQUEST_COLUMNS.filter((c) => !c.optional || requests.some((r) => r[c.key] != null));
  table.tHead.rows[0].replaceChildren(
    ...columns.map((c) => el('th', c.num ? { class: 'num' } : {}, c.title)));
  const shown = $('show-all').checked ? requests : requests.slice(-RECENT_ROWS);
  const rows = [];
  for (let i = shown.length - 1; i >= 0; i -= 1) {
    const r = shown[i];
    rows.push(el('tr', {}, ...columns.map((c) => {
      const classes = [c.num ? 'num' : '', c.bad && c.bad(r) ? 'bad' : ''].filter(Boolean).join(' ');
      const attrs = classes ? { class: classes } : {};
      const tip = c.tip ? c.tip(r) : '';
      if (tip) attrs.title = tip;
      return el('td', attrs, c.cell(r));
    })));
  }
  table.tBodies[0].replaceChildren(...rows);
  $('requests-empty').hidden = requests.length > 0;
}

// ---------------------------------------------------------------- totals and footer

function renderTotals() {
  const totals = state.totals ?? {};
  const log = state.log_totals ?? {};
  const items = [
    ['Prompt tokens prefilled', fmt(totals.prompt_tokens, 0)],
    ['Prompt tokens from cache', fmt(totals.cached_tokens, 0)],
    ['Tokens generated', fmt(totals.generated_tokens, 0)],
    ['Cache reuse overall', totals.cache_reuse_pct == null ? '—' : `${fmt(totals.cache_reuse_pct, 1)} %`],
    ['Draft acceptance overall', totals.accept_pct == null ? '—' : `${fmt(totals.accept_pct, 1)} %`],
    ['GPU device lost', fmt(totals.device_lost, 0)],
    ['Requests in log', fmt(log.requests, 0)],
    ['Errors in log (4xx/5xx)', fmt(log.errors, 0)],
  ];
  $('totals').replaceChildren(...items.map(([label, value]) =>
    el('div', {}, el('dt', {}, label), el('dd', {}, value))));

  const props = Object.values(state.props ?? {})[0] ?? {};
  const parts = [];
  if (props.build) parts.push(`Server ${props.build}`);
  parts.push('token counters since server start; log counts since monitor start');
  if (state.monitor_started) parts.push(`monitor started ${fmtTime(state.monitor_started)}`);
  if (state.sample?.t) parts.push(`updated ${fmtTime(state.sample.t)}`);
  $('footer').textContent = parts.join(' · ');
}

// ---------------------------------------------------------------- data flow

function render() {
  renderQueued = false;
  if (!state) return;
  renderPill();
  renderCards();
  renderSessions();
  renderRequests();
  renderTotals();
  window.GufoAdmin.renderKeys();  // per-key usage arrives with every tick
}

function scheduleRender() {
  if (!renderQueued) {
    renderQueued = true;
    requestAnimationFrame(render);
  }
}

// The monitor is the admin's alone: a 401 means the admin session expired or ended
// elsewhere. The data stops and the page says so, with a link to sign in again.
function sessionExpired() {
  if (expired) return;
  expired = true;
  stopPeek();
  source?.close();
  $('expired').hidden = false;
  window.GufoAdmin.load().catch(() => {});  // the key and account panels close too
  scheduleRender();
}

async function loadState() {
  const response = await fetch('/api/state', { credentials: 'same-origin', cache: 'no-store' });
  if (response.status === 401) {
    sessionExpired();
    return false;
  }
  if (!response.ok) throw new Error(`state ${response.status}`);
  state = await response.json();
  requests = state.requests ?? [];
  requestsDirty = true;
  scheduleRender();
  return true;
}

function applyTick(tick) {
  if (!state) return;
  const lastSeq = requests.length ? requests[requests.length - 1].seq : 0;
  const fresh = (tick.new_requests ?? []).filter((r) => r.seq > lastSeq);
  if (tick.last_request_seq > lastSeq && (!fresh.length || fresh[0].seq !== lastSeq + 1)) {
    loadState().catch(() => {});  // missed requests: take a full snapshot
    return;
  }
  if (fresh.length) {
    requests.push(...fresh);
    if (requests.length > 500) requests.splice(0, requests.length - 500);
    requestsDirty = true;
  }
  for (const key of ['status', 'models', 'props', 'slots', 'sample', 'totals', 'log_totals',
                     'key_usage', 'key_reload', 'seq']) {
    state[key] = tick[key];
  }
  const history = state.history;
  history.t.push(tick.sample.t);
  for (const key of Object.keys(history)) {
    if (key !== 't') history[key].push(tick.sample[key] ?? null);
    if (history[key].length > state.history_len) history[key].shift();
  }
  scheduleRender();
}

function connect() {
  if (expired) return;
  source = new EventSource('/api/stream');
  source.addEventListener('open', () => {
    const wasDown = !streamUp;
    streamUp = true;
    if (wasDown) loadState().catch(() => {});
  });
  source.addEventListener('tick', (event) => applyTick(JSON.parse(event.data)));
  source.addEventListener('error', () => {
    streamUp = false;
    scheduleRender();
    if (source.readyState === EventSource.CLOSED) {
      // Closed for good (e.g. 401 once the admin session ends): check, then retry.
      loadState().then((ok) => ok && setTimeout(connect, 2000)).catch(() => setTimeout(connect, 5000));
    }
  });
}

// ---------------------------------------------------------------- request content
// What Gufo holds with --peek-sessions: queued and running requests and the last finished
// ones, each with the end of its prompt and the text generated so far. Fetched only while
// "Show content" is ticked; the monitor keeps nothing. Text goes in with textContent only.

const PEEK_MS = 2000;
let peekEpoch = 0;     // bumped whenever the section stops, so a late response can tell it is stale
let peekAbort = null;  // the fetch in flight, aborted on stop
let peekTimer = null;
let peekBusy = false;
const peekOpen = new Set();  // request ids the admin expanded, kept across refreshes

function peekSummary(entry) {
  const parts = [entry.request || `generation ${entry.generation}`, entry.client || 'anonymous'];
  if (entry.model) parts.push(entry.model);
  parts.push(`${fmt(entry.prompt_tokens, 0)} in`, `${fmt(entry.generated_tokens, 0)} out`);
  return parts.join(' · ');
}

function peekBlock(title, text, truncated) {
  const pre = el('pre', { class: 'peek-text' });
  pre.textContent = text || '(empty)';
  const note = truncated ? ' (only the end is kept)' : '';
  return el('div', {}, el('div', { class: 'muted' }, title + note), pre);
}

function peekRowOf(entry) {
  const state = entry.state === 'finished' ? entry.finish : entry.state;
  const node = el('details', { class: 'peek-entry' },
    el('summary', {}, el('span', { class: `peek-state state-${entry.state}` }, state), ' ', peekSummary(entry)),
    peekBlock('Prompt', entry.prompt, entry.prompt_truncated),
    peekBlock('Output', entry.output, entry.output_truncated));
  node.open = peekOpen.has(entry.generation);
  node.addEventListener('toggle', () => {
    if (node.open) peekOpen.add(entry.generation);
    else peekOpen.delete(entry.generation);
  });
  return node;
}

function renderPeek(data) {
  const list = $('peek-list');
  const entries = data?.data ?? [];
  if (!entries.length) {
    list.replaceChildren(el('p', { class: 'muted' }, 'No requests held. Is Gufo started with --peek-sessions?'));
    return;
  }
  list.replaceChildren(...entries.map(peekRowOf));
}

async function refreshPeek() {
  if (peekBusy) return;
  peekBusy = true;
  const epoch = peekEpoch;
  const controller = new AbortController();
  peekAbort = controller;
  // A response that arrives after the section was switched off, or the session ended, is dropped
  // unread: it must never put content back on a page that no longer shows it.
  const current = () => epoch === peekEpoch;
  try {
    const response = await fetch('/api/sessions',
      { credentials: 'same-origin', cache: 'no-store', signal: controller.signal });
    if (!current()) return;
    if (response.status === 401) return sessionExpired();
    const body = await response.json().catch(() => ({}));
    if (!current()) return;
    if (!response.ok) {
      $('peek-list').replaceChildren(el('p', { class: 'bad' }, body.detail ?? `HTTP ${response.status}`));
      return;
    }
    renderPeek(body);
  } catch (error) {
    if (current()) $('peek-list').replaceChildren(el('p', { class: 'bad' }, 'The monitor is not reachable.'));
  } finally {
    if (current()) peekBusy = false;
  }
}

// Off, or the session ended: no timer, no request in flight, nothing of the content left in the page.
function stopPeek() {
  peekEpoch += 1;
  clearInterval(peekTimer);
  peekTimer = null;
  peekAbort?.abort();
  peekAbort = null;
  peekBusy = false;
  peekOpen.clear();
  $('peek-list').replaceChildren();
  $('peek-on').checked = false;
}

function setPeek(on) {
  stopPeek();
  if (!on) return;
  $('peek-on').checked = true;
  refreshPeek();
  peekTimer = setInterval(refreshPeek, PEEK_MS);
}

// ---------------------------------------------------------------- setup
// API keys and accounts live in admin.js (GufoAdmin), fed with this page's state; the
// page header and the admin sign-in in header.js (GufoHeader).

// After a sign-in or sign-out from the header: the page needs the admin session, and
// reloads when the admin signs in again from its own header.
function onSession(session) {
  if (!session.admin) sessionExpired();
  else if (expired) window.location.reload();
}

// /monitor#keys (the profile menu's link from a page without admin sections) scrolls to that section
// once it has loaded and shown itself.
function showRequestedSection(tries = 0) {
  const name = window.location.hash.slice(1);
  if (!['accounts', 'keys', 'pricing', 'usage', 'deposits'].includes(name)) return;
  const id = { accounts: 'accounts-section', keys: 'admin-keys-section', pricing: 'admin-pricing-section',
    usage: 'admin-usage-section', deposits: 'admin-deposits-section' }[name];
  if ($(id).hidden && tries < 20) { setTimeout(() => showRequestedSection(tries + 1), 300); return; }
  showAdminSection(name);
}

function showAdminSection(name) {
  const section = $({ accounts: 'accounts-section', keys: 'admin-keys-section',
    pricing: 'admin-pricing-section', usage: 'admin-usage-section',
    deposits: 'admin-deposits-section' }[name]);
  if (!section.hidden) section.scrollIntoView({ block: 'start' });
}

document.addEventListener('DOMContentLoaded', () => {
  buildCards();
  $('show-all').addEventListener('change', () => {
    requestsDirty = true;
    scheduleRender();
  });
  $('peek-on').addEventListener('change', (event) => setPeek(event.target.checked));
  window.GufoHeader.init({ onSession, onAdminTab: showAdminSection });
  window.GufoHeader.refresh().catch(() => {});
  window.GufoAdmin.init({ state: () => state, onChange: () => window.GufoHeader.refresh().catch(() => {}) });
  // Keys need the state's model list for their choices.
  loadState().then((ok) => ok && connect()).catch(() => connect())
    .finally(() => window.GufoAdmin.load().catch(() => {}).finally(() => showRequestedSection()));
  // The long-window tile changes slowly: once a minute, not with every poll.
  loadLongStats().catch(() => {});
  setInterval(() => loadLongStats().catch(() => {}), LONG_REFRESH_MS);
});
