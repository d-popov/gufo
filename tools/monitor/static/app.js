'use strict';

// Cards: one per live metric, each with a sparkline over the history window.
const CARDS = [
  { key: 'decode_tps', label: 'Decode (all sessions)', unit: 'tok/s', digits: 1 },
  { key: 'prefill_tps', label: 'Prefill (all sessions)', unit: 'tok/s', digits: 0 },
  { key: 'running', label: 'Requests running', digits: 0, sub: (s) => `${fmt(s.queued, 0)} queued` },
  { key: 'cache_reuse_pct', label: 'Cache reuse', unit: '%', digits: 1, max: 100,
    value: (s) => s.cache_reuse_window_pct, sub: () => 'completed requests, last 5 min' },
  { key: 'accept_pct', label: 'Draft acceptance', unit: '%', digits: 1, max: 100,
    value: (s) => s.accept_window_pct, sub: () => 'completed requests, last 5 min' },
  { key: 'gpu_busy_pct', label: 'GPU busy', unit: '%', digits: 0, max: 100,
    sub: (s) => (s.gpu_mhz != null ? `${fmt(s.gpu_mhz, 0)} MHz` : '') },
  { key: 'gtt_used_gb', label: 'GPU memory (GTT)', unit: 'GB', digits: 1,
    sub: (s) => `of ${fmt(s.gtt_total_gb, 1)} GB` },
  { key: 'mem_avail_gb', label: 'Memory available', unit: 'GB', digits: 1,
    sub: (s) => `swap ${fmt(s.swap_used_gb, 1)} of ${fmt(s.swap_total_gb, 1)} GB used` },
  { key: 'gpu_temp_c', label: 'GPU temperature', unit: '°C', digits: 0 },
  { key: 'gpu_power_w', label: 'GPU power', unit: 'W', digits: 0 },
  { key: 'cpu_pct', label: 'CPU', unit: '%', digits: 0, max: 100 },
];

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

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: 'short' });
const clockFormat = new Intl.DateTimeFormat(undefined, { timeStyle: 'medium' });

function fmtTime(t) {
  if (t == null) return '';
  const date = new Date(t * 1000);
  const day = dateFormat.format(date);
  const clock = clockFormat.format(date);
  return day === dateFormat.format(new Date()) ? clock : `${day} ${clock}`;
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
      el('div', { class: 'card-label' }, card.label),
      el('div', { class: 'card-value' }, value, unit),
      sub, chart));
    Object.assign(card, { valueNode: value, unitNode: unit, subNode: sub, area, line, dots });
  }
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

// ---------------------------------------------------------------- pill and header

function renderPill() {
  const pill = $('pill');
  const status = state.status ?? {};
  const slots = state.slots ?? [];
  const sample = state.sample ?? {};
  let text;
  let kind;
  if (!streamUp) {
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
  const message = !streamUp ? 'Lost the connection to the monitor; reconnecting.'
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
      state.status?.state === 'ready' ? 'No sessions reported.' : 'Gufo is not ready.')));
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
      return el('td', classes ? { class: classes } : {}, c.cell(r));
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
  if (props.build) parts.push(`Gufo ${props.build}`);
  parts.push('token counters since Gufo start; log counts since monitor start');
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
  renderKeys();
}

function scheduleRender() {
  if (!renderQueued) {
    renderQueued = true;
    requestAnimationFrame(render);
  }
}

async function loadState() {
  const response = await fetch('/api/state', { credentials: 'same-origin', cache: 'no-store' });
  if (response.status === 401) {
    window.location.assign('/login');
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
      // Closed for good (e.g. 401 after logout elsewhere): check, then retry.
      loadState().then((ok) => ok && setTimeout(connect, 2000)).catch(() => setTimeout(connect, 5000));
    }
  });
}

// ---------------------------------------------------------------- API keys
// The list comes from Gufo's key file (GET /api/keys, monitor token); usage
// and reload status arrive with every tick. Changes need the admin token.

let keys = null;  // null: the monitor has no --keys-file
let keysAdmin = false;
let editingKey = null;

async function keysRequest(method, path, body) {
  const options = { method, credentials: 'same-origin', cache: 'no-store', headers: {} };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const text = await response.text();
  const data = text ? JSON.parse(text) : {};
  if (!response.ok) throw new Error(data.error ?? `HTTP ${response.status}`);
  return data;
}

function showKeysError(error) {
  const node = $('keys-error');
  node.textContent = error ? String(error.message ?? error) : '';
  node.hidden = !error;
}

async function loadKeys() {
  const response = await fetch('/api/keys', { credentials: 'same-origin', cache: 'no-store' });
  if (response.status === 404) return;  // key management not configured
  try {
    const data = await response.json();
    if (!response.ok) throw new Error(data.error ?? `HTTP ${response.status}`);
    keys = data.keys;
    keysAdmin = data.admin;
    showKeysError(null);
  } catch (error) {
    keys = keys ?? [];
    showKeysError(error);
  }
  $('keys-section').hidden = false;
  $('admin-unlock').hidden = keysAdmin;
  $('keys-admin').hidden = !keysAdmin;
  renderModelChoices();
  renderKeys();
}

function renderModelChoices() {
  const list = $('key-model-list');
  const checked = new Set([...list.querySelectorAll('input:checked')].map((i) => i.value));
  const names = new Set((state?.models ?? []).map((m) => m.id));
  for (const key of keys ?? []) for (const model of key.models) names.add(model);
  list.replaceChildren(...[...names].sort().map((name) => {
    const box = el('input', { type: 'checkbox', value: name });
    box.checked = checked.has(name);
    return el('label', {}, box, ` ${name}`);
  }));
  syncModelControls();
}

function syncModelControls() {
  const monitor = $('key-access').value === 'monitor';
  $('key-models').disabled = monitor;
  const all = $('key-all-models').checked;
  for (const box of $('key-model-list').querySelectorAll('input')) box.disabled = all || monitor;
}

function renderKeys() {
  if (keys === null) return;
  const usage = state?.key_usage ?? {};
  const reload = state?.key_reload;
  $('keys-reload').textContent = !reload ? ''
    : reload.ok ? `Gufo loaded ${fmt(reload.keys, 0)} keys · ${fmtTime(reload.t)}`
      : `Gufo kept its previous keys: ${reload.reason} · ${fmtTime(reload.t)}`;
  $('keys-reload').className = reload && !reload.ok ? 'bad' : 'muted';
  const rows = keys.map((key) => {
    const used = usage[key.name] ?? {};
    const actions = el('td');
    if (keysAdmin) {
      const edit = el('button', { type: 'button', class: 'link' }, 'Edit');
      edit.addEventListener('click', () => startEdit(key));
      const revoke = el('button', { type: 'button', class: 'link danger' }, 'Revoke');
      revoke.addEventListener('click', () => revokeKey(key.name));
      actions.append(edit, ' ', revoke);
    }
    return el('tr', {},
      el('td', {}, key.name),
      el('td', {}, key.access === 'monitor' ? 'Monitoring' : 'Inference'),
      el('td', {}, key.access === 'monitor' ? '—' : key.models.length ? key.models.join(', ') : 'All'),
      el('td', { class: 'num' }, fmt(used.requests ?? 0, 0)),
      el('td', { class: 'num' }, fmt(used.prompt_tokens ?? 0, 0)),
      el('td', { class: 'num' }, fmt(used.cached_tokens ?? 0, 0)),
      el('td', { class: 'num' }, fmt(used.generated_tokens ?? 0, 0)),
      el('td', { class: used.errors ? 'num bad' : 'num' }, fmt(used.errors ?? 0, 0)),
      el('td', {}, used.last_t ? fmtTime(used.last_t) : '—'),
      actions);
  });
  if (!rows.length) {
    rows.push(el('tr', {}, el('td', { colspan: '10', class: 'muted' }, 'No keys in the file.')));
  }
  $('keys').tBodies[0].replaceChildren(...rows);
}

function resetKeyForm() {
  editingKey = null;
  $('key-form').reset();
  $('key-name').disabled = false;
  $('key-form-title').textContent = 'New key';
  $('key-submit').textContent = 'Create key';
  $('key-cancel').hidden = true;
  for (const box of $('key-model-list').querySelectorAll('input')) box.checked = false;
  syncModelControls();
}

function startEdit(key) {
  editingKey = key.name;
  $('key-name').value = key.name;
  $('key-name').disabled = true;
  $('key-access').value = key.access;
  $('key-all-models').checked = key.models.length === 0;
  for (const box of $('key-model-list').querySelectorAll('input')) {
    box.checked = key.models.includes(box.value);
  }
  $('key-form-title').textContent = `Edit ${key.name}`;
  $('key-submit').textContent = 'Save';
  $('key-cancel').hidden = false;
  syncModelControls();
  $('key-form').scrollIntoView({ block: 'nearest' });
}

async function submitKey(event) {
  event.preventDefault();
  const access = $('key-access').value;
  const models = access === 'monitor' || $('key-all-models').checked ? []
    : [...$('key-model-list').querySelectorAll('input:checked')].map((box) => box.value);
  if (access === 'inference' && !$('key-all-models').checked && !models.length) {
    showKeysError('Choose at least one model, or All models.');
    return;
  }
  try {
    if (editingKey) {
      await keysRequest('PUT', `/api/keys/${encodeURIComponent(editingKey)}`, { access, models });
    } else {
      const created = await keysRequest('POST', '/api/keys', { name: $('key-name').value, access, models });
      $('key-secret-name').textContent = created.name;
      $('key-secret-token').textContent = created.token;
      $('key-secret').hidden = false;
    }
    resetKeyForm();
    showKeysError(null);
  } catch (error) {
    showKeysError(error);
  }
  await loadKeys();
}

async function revokeKey(name) {
  if (!window.confirm(`Revoke key "${name}"? Clients using it are refused within seconds.`)) return;
  try {
    await keysRequest('DELETE', `/api/keys/${encodeURIComponent(name)}`);
    if (editingKey === name) resetKeyForm();
    showKeysError(null);
  } catch (error) {
    showKeysError(error);
  }
  await loadKeys();
}

function setUpKeys() {
  $('admin-unlock').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await keysRequest('POST', '/api/admin/login', { token: $('admin-token').value });
      $('admin-token').value = '';
      showKeysError(null);
    } catch (error) {
      showKeysError(error);
    }
    await loadKeys();
  });
  $('admin-lock').addEventListener('click', async () => {
    await keysRequest('POST', '/api/admin/logout').catch(() => {});
    resetKeyForm();
    await loadKeys();
  });
  $('key-form').addEventListener('submit', submitKey);
  $('key-cancel').addEventListener('click', resetKeyForm);
  $('key-access').addEventListener('change', syncModelControls);
  $('key-all-models').addEventListener('change', syncModelControls);
  $('key-secret-copy').addEventListener('click', () => {
    navigator.clipboard?.writeText($('key-secret-token').textContent).catch(() => {});
  });
  $('key-secret-done').addEventListener('click', () => {
    $('key-secret-token').textContent = '';
    $('key-secret').hidden = true;
  });
  loadKeys().catch(() => {});
  // Picks up edits made outside the page and the hour-long admin unlock ending.
  setInterval(() => loadKeys().catch(() => {}), 30000);
}

document.addEventListener('DOMContentLoaded', () => {
  buildCards();
  $('show-all').addEventListener('change', () => {
    requestsDirty = true;
    scheduleRender();
  });
  // Keys need the state's model list for their choices.
  loadState().then((ok) => ok && connect()).catch(() => connect()).finally(setUpKeys);
});
