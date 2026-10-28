'use strict';

// Admin panels shared by the monitor page (/monitor) and the portal page (/): Gufo's
// API keys, the credits-portal accounts and the price list. Both pages carry the same
// markup (ids admin-key*, accounts*, credit-*, admin-pricing-*) and call
// GufoAdmin.init(). The monitor page passes its live state, for per-key usage and the
// model list; elsewhere this file fetches /api/state itself. Every route here needs the
// admin sign-in (cookie from /api/admin/login, which the shared page header, header.js,
// posts); a 401 means it ended.

window.GufoAdmin = (() => {
  const $ = (id) => document.getElementById(id);

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs)) {
      if (name === 'class') node.className = value;
      else node.setAttribute(name, value);
    }
    for (const child of children) node.append(child);
    return node;
  }

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

  function fmtUsd(micro) {
    if (micro == null || !Number.isFinite(micro)) return '—';
    const abs = Math.abs(micro);
    let frac = String(abs % 1e6).padStart(6, '0').replace(/0+$/, '');
    if (frac.length < 2) frac = frac.padEnd(2, '0');
    return `${micro < 0 ? '-' : ''}$${fmt(Math.floor(abs / 1e6), 0)}.${frac}`;
  }

  // "-2.5" -> -2500000 micro-USD; null unless a plain decimal with at most 6 places.
  function parseUsd(text) {
    const match = /^\s*([-+])?\$?\s*(\d{1,9})(?:\.(\d{1,6}))?\s*$/.exec(text ?? '');
    if (!match) return null;
    const micro = Number(match[2]) * 1e6 + Number((match[3] ?? '').padEnd(6, '0'));
    return match[1] === '-' ? -micro : micro;
  }

  function copy(button, text) {
    navigator.clipboard?.writeText(text).then(() => {
      const label = button.textContent;
      button.textContent = 'Copied';
      setTimeout(() => { button.textContent = label; }, 1500);
    }).catch(() => {});
  }

  // { state: () => monitor state | null, active: () => bool, onChange: (admin) => void }
  let options = {};
  let fetchedState = null;
  let keys = null;  // null: the monitor has no --keys-file
  let keysAdmin = false;
  let editingKey = null;
  let accounts = null;
  let adminUsage = null;  // internal (admin key) usage totals, last 30 days; null: unavailable
  let creditFor = null;
  let pricing = null;  // the stored table from /api/admin/pricing; null: none loaded
  let priceRows = [];  // what the form shows: [{ name: null (Default) | model, text: { field: '0.30' } }]
  let pricingNote = null;  // a problem from adding a model or saving, shown with the field errors

  const currentState = () => (options.state ? options.state() : fetchedState);

  async function request(method, path, body) {
    const init = { method, credentials: 'same-origin', cache: 'no-store', headers: {} };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const response = await fetch(path, init);
    const text = await response.text();
    let data = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = {};
    }
    if (!response.ok) {
      const error = new Error(response.status === 401 ? 'Admin session expired, sign in again.'
        : data.message ?? data.error ?? `HTTP ${response.status}`);
      error.status = response.status;
      if (response.status === 401) loadKeys().catch(() => {});  // hides the panels, tells the page
      throw error;
    }
    return data;
  }

  // ---------------------------------------------------------------- API keys
  // The list comes from Gufo's key file (GET /api/keys); usage and reload status
  // come with the monitor state.

  function showKeysError(error) {
    const node = $('admin-keys-error');
    node.textContent = error ? String(error.message ?? error) : '';
    node.hidden = !error;
  }

  async function load() {
    if (!options.state) {
      fetchedState = await request('GET', '/api/state').catch(() => fetchedState);
    }
    await loadKeys();
  }

  async function loadKeys() {
    const wasAdmin = keysAdmin;
    const response = await fetch('/api/keys', { credentials: 'same-origin', cache: 'no-store' });
    if (response.status === 404) return;  // key management not configured
    keysAdmin = response.status !== 401;
    try {
      const data = await response.json();
      if (!response.ok) throw new Error(data.error ?? `HTTP ${response.status}`);
      keys = data.keys;
      showKeysError(null);
    } catch (error) {
      keys = keys ?? [];
      showKeysError(error);
    }
    $('admin-keys-section').hidden = !keysAdmin;
    $('admin-keys-manage').hidden = !keysAdmin;
    renderModelChoices();
    renderKeys();
    await loadAccounts();
    await loadPricing();
    await loadUsage();
    if (wasAdmin !== keysAdmin) options.onChange?.(keysAdmin);
  }

  function renderModelChoices() {
    const list = $('admin-key-model-list');
    const checked = new Set([...list.querySelectorAll('input:checked')].map((i) => i.value));
    const names = new Set((currentState()?.models ?? []).map((m) => m.id));
    for (const key of keys ?? []) for (const model of key.models) names.add(model);
    list.replaceChildren(...[...names].sort().map((name) => {
      const box = el('input', { type: 'checkbox', value: name });
      box.checked = checked.has(name);
      return el('label', {}, box, ` ${name}`);
    }));
    syncModelControls();
  }

  function syncModelControls() {
    const monitor = $('admin-key-access').value === 'monitor';
    $('admin-key-models').disabled = monitor;
    const all = $('admin-key-all-models').checked;
    for (const box of $('admin-key-model-list').querySelectorAll('input')) box.disabled = all || monitor;
  }

  function renderKeys() {
    if (keys === null) return;
    const state = currentState();
    const usage = state?.key_usage ?? {};
    const reload = state?.key_reload;
    $('admin-keys-reload').textContent = !reload ? ''
      : reload.ok ? `Gufo loaded ${fmt(reload.keys, 0)} keys · ${fmtTime(reload.t)}`
        : `Gufo kept its previous keys: ${reload.reason} · ${fmtTime(reload.t)}`;
    $('admin-keys-reload').className = reload && !reload.ok ? 'bad' : 'muted';
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
    $('admin-keys').tBodies[0].replaceChildren(...rows);
  }

  function resetKeyForm() {
    editingKey = null;
    $('admin-key-form').reset();
    $('admin-key-name').disabled = false;
    $('admin-key-form-title').textContent = 'New key';
    $('admin-key-submit').textContent = 'Create key';
    $('admin-key-cancel').hidden = true;
    for (const box of $('admin-key-model-list').querySelectorAll('input')) box.checked = false;
    syncModelControls();
  }

  function startEdit(key) {
    editingKey = key.name;
    $('admin-key-name').value = key.name;
    $('admin-key-name').disabled = true;
    $('admin-key-access').value = key.access;
    $('admin-key-all-models').checked = key.models.length === 0;
    for (const box of $('admin-key-model-list').querySelectorAll('input')) {
      box.checked = key.models.includes(box.value);
    }
    $('admin-key-form-title').textContent = `Edit ${key.name}`;
    $('admin-key-submit').textContent = 'Save';
    $('admin-key-cancel').hidden = false;
    syncModelControls();
    $('admin-key-form').scrollIntoView({ block: 'nearest' });
  }

  async function submitKey(event) {
    event.preventDefault();
    const access = $('admin-key-access').value;
    const models = access === 'monitor' || $('admin-key-all-models').checked ? []
      : [...$('admin-key-model-list').querySelectorAll('input:checked')].map((box) => box.value);
    if (access === 'inference' && !$('admin-key-all-models').checked && !models.length) {
      showKeysError('Choose at least one model, or All models.');
      return;
    }
    try {
      if (editingKey) {
        await request('PUT', `/api/keys/${encodeURIComponent(editingKey)}`, { access, models });
      } else {
        const created = await request('POST', '/api/keys', { name: $('admin-key-name').value, access, models });
        $('admin-key-secret-name').textContent = created.name;
        $('admin-key-secret-token').textContent = created.token;
        $('admin-key-secret').hidden = false;
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
      await request('DELETE', `/api/keys/${encodeURIComponent(name)}`);
      if (editingKey === name) resetKeyForm();
      showKeysError(null);
    } catch (error) {
      showKeysError(error);
    }
    await loadKeys();
  }

  // ---------------------------------------------------------------- accounts (credits portal)
  // Only for the admin: the list has emails and verification links. Emails are chosen
  // by strangers, so everything goes in as text nodes.

  function showAccountsNote(id, value) {
    const node = $(id);
    node.textContent = value ? String(value.message ?? value) : '';
    node.hidden = !value;
  }

  async function loadAccounts() {
    if (!keysAdmin) {
      accounts = null;
      hideCreditForm();
      $('accounts-section').hidden = true;
      return;
    }
    const response = await fetch('/api/admin/accounts', { credentials: 'same-origin', cache: 'no-store' });
    if (response.status === 404) return;  // no --billing-config
    try {
      const data = await response.json();
      if (!response.ok) throw new Error(data.error ?? `HTTP ${response.status}`);
      accounts = data;
      showAccountsNote('accounts-error', null);
    } catch (error) {
      showAccountsNote('accounts-error', error);
    }
    $('accounts-section').hidden = false;
    adminUsage = null;
    try {
      adminUsage = (await request('GET', '/api/admin/usage?days=30')).internal?.totals ?? null;
    } catch (error) {
      // Usage is optional here; the Usage section reports its own errors.
    }
    renderAccounts();
  }

  const ACCOUNT_STATUS = {
    pending_verification: ['Email unverified', 'state-prompt'],
    pending_approval: ['Awaiting approval', 'state-prompt'],
    approved: ['Approved', 'state-busy'],
    rejected: ['Rejected', 'bad'],
    suspended: ['Suspended', 'bad'],
  };

  function renderAccounts() {
    const list = accounts?.accounts ?? [];
    const totals = accounts?.totals ?? {};
    const usage = adminUsage;
    const usageCards = usage ? [
      ['Your usage, 30 d (not charged)', fmtUsd(usage.cost_micro)],
      ['Your requests / tokens', `${fmt(usage.requests, 0)} / ${fmt(usage.prompt + usage.generated, 0)}`],
    ] : [];
    $('accounts-totals').replaceChildren(...[
      ['Users', fmt(totals.users, 0)],
      ['Awaiting approval', fmt(totals.pending_approval, 0)],
      ['Balances', fmtUsd(totals.balance_micro)],
      ['Deposited', fmtUsd(totals.deposited_micro)],
      ['Spent', fmtUsd(totals.spent_micro)],
      ...usageCards,
    ].map(([label, value]) => el('div', {}, el('dt', {}, label), el('dd', {}, value))));

    const rows = list.map((account) => {
      const email = el('td', { class: 'wrap' }, account.email);
      if (account.verification_link) {
        const copyButton = el('button', { type: 'button', class: 'link' }, 'Copy');
        copyButton.addEventListener('click', () => copy(copyButton, account.verification_link));
        email.append(el('div', { class: 'verify-link' },
          el('span', { class: 'muted' }, 'Verification link: '),
          el('code', {}, account.verification_link), ' ', copyButton));
      }
      const [statusText, statusClass] = ACCOUNT_STATUS[account.status] ?? [account.status, ''];
      const actions = el('td', { class: 'account-actions' });
      const button = (label, onClick, extra = {}) => {
        const node = el('button', { type: 'button', class: extra.danger ? 'link danger' : 'link' }, label);
        if (extra.disabled) {
          node.disabled = true;
          node.title = extra.disabled;
        }
        node.addEventListener('click', onClick);
        actions.append(node);
      };
      const act = (action) => () => accountAction(account, action);
      if (!account.email_verified) button('Verify', act('verify'));
      if (['pending_verification', 'pending_approval', 'rejected'].includes(account.status)) {
        button('Approve', act('approve'),
          { disabled: account.email_verified ? null : 'The email address is not verified yet.' });
      }
      if (account.status === 'pending_verification' || account.status === 'pending_approval') {
        button('Reject', act('reject'), { danger: true });
      }
      if (account.status === 'approved') button('Suspend', act('suspend'), { danger: true });
      if (account.status === 'suspended') button('Unsuspend', act('unsuspend'));
      if (account.status === 'approved' || account.status === 'suspended') {
        button('Add credit', () => showCreditForm(account));
      }
      return el('tr', {},
        el('td', {}, String(account.id)),
        email,
        el('td', { class: statusClass }, statusText),
        el('td', {}, account.email_verified ? 'Yes' : 'No'),
        el('td', { class: account.balance_micro <= 0 ? 'num bad' : 'num' }, fmtUsd(account.balance_micro)),
        el('td', { class: 'num' }, fmtUsd(account.spent_micro)),
        el('td', { class: 'num' }, fmtUsd(account.deposited_micro)),
        el('td', { class: 'num' }, fmt(account.keys, 0)),
        el('td', { class: 'wrap' }, ...(account.wallets ?? []).map((w) =>
          el('div', {}, `${w.chain}: ${w.address}`))),
        el('td', {}, fmtTime(account.created)),
        actions);
    });
    if (usage) {
      rows.unshift(el('tr', {},
        el('td', {}, '—'),
        el('td', { class: 'wrap' }, 'You (admin keys, last 30 days)'),
        el('td', { class: 'muted' }, 'Not charged'),
        el('td', {}, '—'),
        el('td', { class: 'num' }, '—'),
        el('td', { class: 'num' }, fmtUsd(usage.cost_micro)),
        el('td', { class: 'num' }, '—'),
        el('td', { class: 'num' }, `${fmt(usage.requests, 0)} req`),
        el('td', {}),
        el('td', {}),
        el('td', {})));
    }
    if (!list.length) {
      rows.push(el('tr', {}, el('td', { colspan: '11', class: 'muted' }, 'No accounts yet.')));
    }
    $('accounts').tBodies[0].replaceChildren(...rows);
  }

  const DONE = {
    verify: 'Verified the email address of',
    reject: 'Rejected',
    suspend: 'Suspended',
    unsuspend: 'Unsuspended',
  };

  async function accountAction(account, action, body = {}) {
    if ((action === 'reject' || action === 'suspend') && !window.confirm(
      `${action === 'reject' ? 'Reject' : 'Suspend'} ${account.email}? Their API keys stop working.`)) return false;
    let message = null;
    try {
      const result = await request('POST',
        `/api/admin/accounts/${encodeURIComponent(account.id)}/${action}`, body);
      showAccountsNote('accounts-error', null);
      await loadAccounts();
      if (action === 'approve') {
        const now = accounts?.accounts?.find((a) => a.id === account.id);
        const grant = result.grant_micro ?? (now ? now.balance_micro - account.balance_micro : null);
        message = `Approved ${account.email}: granted ${fmtUsd(grant)}.`;
      } else if (action === 'credit') {
        message = `${body.amount_micro < 0 ? 'Debited' : 'Credited'} ${fmtUsd(Math.abs(body.amount_micro))} `
          + `${body.amount_micro < 0 ? 'from' : 'to'} ${account.email}.`;
      } else {
        message = `${DONE[action]} ${account.email}.`;
      }
    } catch (error) {
      showAccountsNote('accounts-error', error);
      await loadAccounts().catch(() => {});
      return false;
    }
    showAccountsNote('accounts-message', message);
    return true;
  }

  function showCreditForm(account) {
    creditFor = account;
    $('credit-title').textContent = `Credit #${account.id} ${account.email}:`;
    $('credit-form').hidden = false;
    $('credit-amount').focus();
    $('credit-form').scrollIntoView({ block: 'nearest' });
  }

  function hideCreditForm() {
    creditFor = null;
    $('credit-form').reset();
    $('credit-form').hidden = true;
  }

  // ---------------------------------------------------------------- pricing (credits portal)
  // The whole table goes back with PUT /api/admin/pricing. Prices are stored as integer
  // micro-USD per 1M tokens and edited as dollars, converted from the decimal text
  // without floating point.

  const PRICE_FIELDS = ['input', 'cached_input', 'output'];
  const PRICE_LABELS = { input: 'Input', cached_input: 'Cached input', output: 'Output' };
  const MAX_PRICE = 1e12;  // micro-USD per 1M tokens, as billing/pricing.py
  const MAX_MODELS = 64;
  const MAX_MODEL_NAME = 128;
  // The example request: 1000 prompt tokens, 200 of them cached, 100 output tokens.
  const EXAMPLE = { prompt: 1000, cached: 200, output: 100 };

  // 225000 -> "0.225": dollars per 1M tokens, exact, at least two decimals.
  function microToDollars(micro) {
    let frac = String(micro % 1e6).padStart(6, '0').replace(/0+$/, '');
    if (frac.length < 2) frac = frac.padEnd(2, '0');
    return `${Math.floor(micro / 1e6)}.${frac}`;
  }

  // "0.30" -> 300000; null unless a non-negative decimal with at most 6 places.
  function dollarsToMicro(text) {
    const match = /^\s*\$?\s*(\d{0,7})(?:\.(\d{0,6}))?\s*$/.exec(text ?? '');
    if (!match || !/\d/.test(match[1] + (match[2] ?? ''))) return null;
    const micro = Number(match[1] || '0') * 1e6 + Number((match[2] ?? '').padEnd(6, '0'));
    return micro <= MAX_PRICE ? micro : null;
  }

  // As billing/pricing.py: cached prompt tokens pay the cached price, rounded up.
  function exampleCost(prices) {
    const scaled = (EXAMPLE.prompt - EXAMPLE.cached) * prices.input
      + EXAMPLE.cached * prices.cached_input + EXAMPLE.output * prices.output;
    return Math.ceil(scaled / 1e6);
  }

  function setPriceRows(table) {
    const row = (name, prices) => ({
      name, text: Object.fromEntries(PRICE_FIELDS.map((f) => [f, microToDollars(prices[f])])) });
    priceRows = [row(null, table.default),
      ...Object.entries(table.models ?? {}).map(([name, prices]) => row(name, prices))];
  }

  // The table the form holds, in micro-USD (null where the text is not a price), and
  // what is wrong with it.
  function readPricing() {
    const errors = [];
    const table = { default: null, models: {} };
    for (const row of priceRows) {
      const prices = {};
      for (const field of PRICE_FIELDS) {
        prices[field] = dollarsToMicro(row.text[field]);
        if (prices[field] == null) {
          errors.push(`${row.name ?? 'Default'}, ${PRICE_LABELS[field]}: enter US dollars per 1M `
            + 'tokens, e.g. 0.30, with at most 6 decimals.');
        }
      }
      if (row.name == null) table.default = prices;
      else table.models[row.name] = prices;
    }
    return { table, errors };
  }

  function pricingDirty() {
    if (!pricing) return false;
    const stored = { default: pricing.default, models: pricing.models };
    return JSON.stringify(readPricing().table) !== JSON.stringify(stored);
  }

  function renderPricing() {
    const rows = priceRows.map((row) => {
      const label = row.name ?? 'Default';
      const cells = PRICE_FIELDS.map((field) => {
        const input = el('input', { inputmode: 'decimal', size: '9', autocomplete: 'off',
          'aria-label': `${label}, ${PRICE_LABELS[field]}, USD per 1M tokens` });
        input.value = row.text[field];
        input.addEventListener('input', () => {
          row.text[field] = input.value;
          pricingNote = null;
          updatePricing();
        });
        return el('td', { class: 'num' }, input);
      });
      const actions = el('td');
      if (row.name != null) {
        const remove = el('button', { type: 'button', class: 'link danger' }, 'Remove');
        remove.addEventListener('click', () => {
          priceRows.splice(priceRows.indexOf(row), 1);
          pricingNote = null;
          renderPricing();
        });
        actions.append(remove);
      }
      return el('tr', {}, el('td', { class: row.name == null ? 'wrap price-default' : 'wrap' }, label),
        ...cells, el('td', { class: 'num price-example' }), actions);
    });
    $('admin-pricing-table').tBodies[0].replaceChildren(...rows);
    renderServed();
    updatePricing();
  }

  // The models Gufo serves that have no row yet, offered when adding one.
  function renderServed() {
    const priced = new Set(priceRows.map((row) => row.name));
    $('admin-pricing-served').replaceChildren(...(pricing?.served_models ?? [])
      .filter((name) => !priced.has(name)).map((name) => el('option', { value: name })));
  }

  // Marks bad fields, recomputes the examples and the buttons; keeps focus in the form.
  function updatePricing() {
    const body = $('admin-pricing-table').tBodies[0];
    priceRows.forEach((row, index) => {
      const tr = body.rows[index];
      const inputs = tr.querySelectorAll('input');
      const prices = {};
      PRICE_FIELDS.forEach((field, i) => {
        prices[field] = dollarsToMicro(row.text[field]);
        inputs[i].classList.toggle('invalid', prices[field] == null);
        inputs[i].setAttribute('aria-invalid', String(prices[field] == null));
      });
      tr.querySelector('.price-example').textContent =
        PRICE_FIELDS.every((field) => prices[field] != null) ? fmtUsd(exampleCost(prices)) : '—';
    });
    const { errors } = readPricing();
    const problems = pricingNote ? [...errors, pricingNote] : errors;
    const node = $('admin-pricing-error');
    node.replaceChildren(...problems.map((text) => el('div', {}, text)));
    node.hidden = !problems.length;
    const dirty = pricingDirty();
    if (dirty) showAccountsNote('admin-pricing-message', null);
    $('admin-pricing-save').disabled = !dirty || errors.length > 0;
    $('admin-pricing-revert').disabled = !dirty;
    $('admin-pricing-state').textContent = dirty ? 'Unsaved changes' : '';
    $('admin-pricing-add').disabled = !pricing || priceRows.length > MAX_MODELS;
  }

  async function loadPricing() {
    const section = $('admin-pricing-section');
    if (!keysAdmin) {
      pricing = null;
      section.hidden = true;
      return;
    }
    const response = await fetch('/api/admin/pricing', { credentials: 'same-origin', cache: 'no-store' });
    if (response.status === 404) {  // no --billing-config
      section.hidden = true;
      return;
    }
    let replace = false;
    try {
      const data = await response.json();
      if (!response.ok) throw new Error(data.message ?? data.error ?? `HTTP ${response.status}`);
      // The periodic reload leaves the form alone while the admin works in it.
      replace = !pricing || !(pricingDirty()
        || $('admin-pricing-form').contains(document.activeElement));
      pricing = data;
      if (replace) {
        setPriceRows(data);
        pricingNote = null;
      }
    } catch (error) {
      pricingNote = String(error.message ?? error);
    }
    section.hidden = false;
    if (replace) {
      renderPricing();
    } else {
      renderServed();
      updatePricing();
    }
  }

  function addPriceModel() {
    const input = $('admin-pricing-new');
    const name = input.value.trim();
    pricingNote = !name ? 'Enter a model name to add.'
      : name.length > MAX_MODEL_NAME ? `A model name has at most ${MAX_MODEL_NAME} characters.`
        : /\p{C}/u.test(name) ? 'A model name cannot hold control characters.'
          : priceRows.some((row) => row.name === name) ? `${name} already has its own prices.`
            : priceRows.length > MAX_MODELS ? `At most ${MAX_MODELS} models.` : null;
    if (!pricingNote) {
      priceRows.push({ name, text: { ...priceRows[0].text } });  // starts at the Default prices
      input.value = '';
    }
    renderPricing();
  }

  async function savePricing(event) {
    event.preventDefault();
    const { table, errors } = readPricing();
    if (errors.length) {
      updatePricing();
      return;
    }
    try {
      pricing = await request('PUT', '/api/admin/pricing', table);
      setPriceRows(pricing);
      pricingNote = null;
      renderPricing();
      showAccountsNote('admin-pricing-message',
        'Saved. Requests billed from now on use these prices; earlier charges stay as they were.');
    } catch (error) {
      pricingNote = String(error.message ?? error);
      updatePricing();
    }
  }

  function revertPricing() {
    if (!pricing) return;
    setPriceRows(pricing);
    pricingNote = null;
    renderPricing();
  }

  // ---------------------------------------------------------------- usage (no charge for internal keys)
  let usageSequence = 0;

  async function loadUsage() {
    const section = $('admin-usage-section');
    if (!keysAdmin) {
      section.hidden = true;
      return;
    }
    const sequence = ++usageSequence;
    const days = $('admin-usage-days').value;
    try {
      const data = await request('GET', `/api/admin/usage?days=${days}`);
      if (sequence !== usageSequence) return;
      section.hidden = false;
      showAccountsNote('admin-usage-error', null);
      renderUsage(data);
      loadLoad();
    } catch (error) {
      if (sequence !== usageSequence) return;
      section.hidden = error.status === 404 || error.status === 401;
      showAccountsNote('admin-usage-error', error);
    }
  }

  // Server load over the last 24 h / 7 days (GET /api/stats/long, monitor-side) with
  // revenue averaged over each window: users' payments plus our own (internal) requests at list price.
  // r: {users, internal, total} micro-dollars of the window; per-hour times `scale` hours.
  const usd = (r, w, scale, part) => (r == null ? '—' : fmtUsd(Math.round(r[part] * scale / w.window_hours)));
  const LOAD_ROWS = [
    ['Decode tok/s', (w) => fmt(w.decode_tps, 1)],
    ['Prefill tok/s', (w) => fmt(w.prefill_tps, 0)],
    ['Tokens/h', (w) => fmt(w.tokens_per_hour, 0)],
    ['Revenue/h (users + internal)', (w, r) => usd(r, w, 1, 'total')],
    ['Revenue/day (users + internal)', (w, r) => usd(r, w, 24, 'total')],
    ['Users: revenue/h', (w, r) => usd(r, w, 1, 'users')],
    ['Users: revenue/day', (w, r) => usd(r, w, 24, 'users')],
    ['Internal: revenue/h (not charged)', (w, r) => usd(r, w, 1, 'internal')],
    ['Internal: revenue/day (not charged)', (w, r) => usd(r, w, 24, 'internal')],
    ['Revenue in window (users + internal)', (w, r) => (r == null ? '—' : fmtUsd(r.total))],
    ['Requests', (w) => fmt(w.requests, 0)],
    ['Errors', (w) => fmt(w.errors, 0)],
    ['Cache reuse %', (w) => fmt(w.cache_reuse_pct, 1)],
    ['Serving time %', (w) => fmt(w.serving_pct, 1)],
    ['GPU busy %', (w) => fmt(w.gpu_busy_pct, 0)],
    ['GPU power W', (w) => fmt(w.gpu_power_w, 0)],
  ];

  async function loadLoad() {
    const table = $('admin-load');
    if (!table) return;  // the monitor page shows these figures in its tile
    const body = table.tBodies[0];
    let stats = null;
    try {
      stats = await request('GET', '/api/stats/long');
    } catch (error) {
      // Optional: the monitor may run without --stats-file.
    }
    const day = stats?.windows?.['24h'];
    const week = stats?.windows?.['168h'];
    body.replaceChildren(...(stats ? LOAD_ROWS.map(([label, cell]) => el('tr', {},
      el('th', { scope: 'row' }, label),
      el('td', { class: 'num' }, cell(day, stats.revenue_micro?.['24h'])),
      el('td', { class: 'num' }, cell(week, stats.revenue_micro?.['168h']))))
      : [el('tr', {}, el('td', { colspan: '3', class: 'muted' }, 'No long-window stats.'))]));
    const notes = [];
    if (stats && stats.since != null && stats.since > week.start + 60) {
      notes.push(`Log data since ${fmtTime(stats.since)}: averages cover that span only.`);
    }
    if (stats && week.gpu_hours < (stats.now - week.start) / 3600 * 0.99) {
      notes.push(`GPU figures cover ${fmt(day.gpu_hours, 1)} h (24 h) and ${fmt(week.gpu_hours, 1)} h (7 d) `
        + 'of polled time; the log has no GPU samples.');
    }
    if (stats && stats.revenue_micro == null) notes.push('Revenue needs billing.');
    showAccountsNote('admin-load-note', notes.length ? notes.join(' ') : null);
  }

  function renderUsage(data) {
    const internal = data.internal.totals;
    const users = data.users.totals;
    const total = internal.cost_micro + users.cost_micro;
    const tokens = (row) => row.prompt + row.generated;  // cached is part of prompt
    $('admin-usage-totals').replaceChildren(...[
      ['Internal cost (not charged)', fmtUsd(internal.cost_micro)],
      ["Users' cost", fmtUsd(users.cost_micro)],
      ['Internal share', total ? `${fmt(100 * internal.cost_micro / total, 1)}%` : '—'],
      ['Internal tokens (prompt + output)', fmt(tokens(internal), 0)],
      ['Users’ known tokens (prompt + output)', fmt(tokens(users), 0)],
    ].map(([label, value]) => el('div', {}, el('dt', {}, label), el('dd', {}, value))));
    const keys = data.internal.by_key.map((row) => el('tr', {},
      el('td', { class: 'wrap' }, row.key),
      ...['requests', 'prompt', 'cached', 'generated'].map((field) =>
        el('td', { class: 'num' }, fmt(row[field], 0))),
      el('td', { class: 'num' }, fmtUsd(row.cost_micro))));
    if (!keys.length) keys.push(el('tr', {}, el('td', { colspan: '6', class: 'muted' },
      'No internal usage in this window.')));
    $('admin-usage-keys').tBodies[0].replaceChildren(...keys);
    const byDay = new Map();
    for (const kind of ['internal', 'users']) {
      for (const row of data[kind].by_day) {
        if (!byDay.has(row.day)) byDay.set(row.day, {});
        byDay.get(row.day)[kind] = row;
      }
    }
    const rows = [...byDay].sort(([a], [b]) => b.localeCompare(a)).map(([day, row]) =>
      el('tr', {}, el('td', {}, day),
        ...['internal', 'users'].flatMap((kind) => [
          el('td', { class: 'num' }, fmt(row[kind]?.requests ?? 0, 0)),
          el('td', { class: 'num' }, fmtUsd(row[kind]?.cost_micro ?? 0)),
        ])));
    if (!rows.length) rows.push(el('tr', {}, el('td', { colspan: '5', class: 'muted' },
      'No usage in this window.')));
    $('admin-usage-by-day').tBodies[0].replaceChildren(...rows);
    showAccountsNote('admin-usage-note', users.tokens_unknown_requests
      ? `Tokens unknown for ${fmt(users.tokens_unknown_requests, 0)} earlier user requests: `
        + 'their cost is included, but their tokens were not recorded. Token totals show known tokens only.'
      : null);
  }

  // ---------------------------------------------------------------- setup

  function init(settings = {}) {
    options = settings;
    $('admin-key-form').addEventListener('submit', submitKey);
    $('admin-key-cancel').addEventListener('click', resetKeyForm);
    $('admin-key-access').addEventListener('change', syncModelControls);
    $('admin-key-all-models').addEventListener('change', syncModelControls);
    $('admin-key-secret-copy').addEventListener('click', (event) =>
      copy(event.currentTarget, $('admin-key-secret-token').textContent));
    $('admin-key-secret-done').addEventListener('click', () => {
      $('admin-key-secret-token').textContent = '';
      $('admin-key-secret').hidden = true;
    });
    $('credit-cancel').addEventListener('click', hideCreditForm);
    $('credit-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const amount = parseUsd($('credit-amount').value);
      if (!amount) {
        showAccountsNote('accounts-error', 'Enter a non-zero amount in USD, e.g. 5 or -0.25.');
        return;
      }
      const ok = await accountAction(creditFor, 'credit',
        { amount_micro: amount, note: $('credit-note').value.trim() });
      if (ok) hideCreditForm();
    });
    $('admin-pricing-form').addEventListener('submit', savePricing);
    $('admin-pricing-add').addEventListener('click', addPriceModel);
    $('admin-pricing-new').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        addPriceModel();
      }
    });
    $('admin-pricing-revert').addEventListener('click', revertPricing);
    $('admin-usage-days').addEventListener('change', () => loadUsage());
    // Picks up edits made outside the page and the admin sign-in ending.
    setInterval(() => {
      if (!options.active || options.active()) load().catch(() => {});
    }, 30000);
  }

  return {
    init,
    load,
    renderKeys,
    get admin() { return keysAdmin; },
  };
})();
