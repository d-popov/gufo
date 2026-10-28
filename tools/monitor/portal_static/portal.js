'use strict';

// Public credits portal: account, keys, prices, history. Wallet top-ups live
// in wallet.js (GufoWallet), which this file feeds with the account data.

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

// ---------------------------------------------------------------- money (integer micro-USD)

const fmtUsd = window.GufoHeader.fmtUsd;

// "12.5" -> 12500000; null for anything that is not a plain decimal amount.
function parseUsd(text, { signed = false } = {}) {
  const match = /^\s*([-+])?\$?\s*(\d{1,9})(?:\.(\d{1,6}))?\s*$/.exec(text ?? '');
  if (!match || (match[1] === '-' && !signed)) return null;
  const micro = Number(match[2]) * 1e6 + Number((match[3] ?? '').padEnd(6, '0'));
  return match[1] === '-' ? -micro : micro;
}

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
const fmtTime = (t) => (t == null ? '—' : dateFormat.format(new Date(t * 1000)));

// ---------------------------------------------------------------- API

const ERROR_TEXT = {
  bad_credentials: 'Wrong email or password.',
  rate_limited: 'Too many attempts. Wait a minute and try again.',
  weak_password: 'The password must have at least 10 characters.',
  not_approved: 'This needs an approved account.',
  key_limit: 'You already have the maximum number of keys. Revoke one first.',
  wallet_taken: 'This wallet is already linked to another account.',
  bad_signature: 'The wallet signature did not verify. Try linking again.',
  challenge_expired: 'The linking challenge expired. Try again.',
  not_linked: 'This wallet is not linked to your account yet.',
  no_token_account: 'This wallet has no USDC token account on Solana.',
  bad_amount: 'The amount must be between $1 and $1,000.',
  unauthorized: 'Your session ended. Sign in again.',
};

async function api(method, path, body) {
  const options = { method, credentials: 'same-origin', cache: 'no-store', headers: {} };
  if (method !== 'GET') {
    options.headers['X-Gufo-Portal'] = '1';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body ?? {});
  }
  const response = await fetch(`/api/portal/${path}`, options);
  const text = await response.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = {};
  }
  if (!response.ok) {
    const error = new Error(ERROR_TEXT[data.error] ?? data.message ?? data.error ?? `HTTP ${response.status}`);
    error.status = response.status;
    error.code = data.error;
    throw error;
  }
  return data;
}

function showError(id, error) {
  const node = $(id);
  node.textContent = error ? String(error.message ?? error) : '';
  node.hidden = !error;
}

function showNotice(text) {
  $('notice').textContent = text ?? '';
  $('notice').hidden = !text;
}

function copyText(button, text) {
  const label = button.textContent;
  navigator.clipboard?.writeText(text).then(() => {
    button.textContent = 'Copied';
    setTimeout(() => { button.textContent = label; }, 1500);
  }).catch(() => {});
}

// ---------------------------------------------------------------- account

let me = null;
let portalEnabled = true;  // false: no --billing-config, only the admin sign-in is left

function showAuth() {
  me = null;
  $('auth').hidden = !portalEnabled;
  $('account').hidden = true;
  window.GufoHeader.setUser(null, portalEnabled);
}

async function load() {
  let account;
  try {
    account = await api('GET', 'me');
  } catch (error) {
    if (error.status === 404) {
      portalEnabled = false;
      showNotice('The credits portal is not enabled on this server.');
    }
    if (error.status === 401 || error.status === 404) {
      showAuth();
      return;
    }
    throw error;
  }
  me = account;
  $('auth').hidden = true;
  $('account').hidden = false;
  window.GufoHeader.setUser(me, portalEnabled);
  renderAccount();
  window.GufoWallet?.render(me);
  await loadLedger();
}

// ---------------------------------------------------------------- sign-in tabs
// One form at a time: Sign in (default) or Sign up, also from ?signup and the profile menu.

const AUTH_TABS = ['signin', 'signup'];

function showAuthTab(name, focus = false) {
  for (const tab of AUTH_TABS) {
    const selected = tab === name;
    $(`auth-tab-${tab}`).setAttribute('aria-selected', String(selected));
    $(`auth-tab-${tab}`).tabIndex = selected ? 0 : -1;
    $(`auth-pane-${tab}`).hidden = !selected;
  }
  if (focus) $(`auth-tab-${name}`).focus();
}

// From the profile menu: the form with its first field focused.
function openAuthForm(name) {
  showAuthTab(name);
  $('auth').scrollIntoView({ block: 'nearest' });
  $(name === 'signup' ? 'signup-email' : 'login-email').focus();
}

function setUpAuthTabs() {
  for (const tab of AUTH_TABS) {
    $(`auth-tab-${tab}`).addEventListener('click', () => showAuthTab(tab));
    $(`auth-tab-${tab}`).addEventListener('keydown', (event) => {
      const index = AUTH_TABS.indexOf(tab);
      const next = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: AUTH_TABS.length - 1 }[event.key];
      if (next === undefined) return;
      event.preventDefault();
      showAuthTab(AUTH_TABS[(next + AUTH_TABS.length) % AUTH_TABS.length], true);
    });
  }
  for (const link of document.querySelectorAll('[data-auth-tab]')) {
    link.addEventListener('click', () => openAuthForm(link.dataset.authTab));
  }
}

// ---------------------------------------------------------------- admin area
// The admin signs in from the profile menu (header.js); the session says what to show.

const ADMIN_TABS = ['accounts', 'keys', 'pricing', 'usage'];
const BILLING_TABS = ['accounts', 'pricing', 'usage'];  // need the credits portal

function showTab(name) {
  if (BILLING_TABS.includes(name) && !portalEnabled) name = 'keys';
  for (const tab of ADMIN_TABS) {
    $(`tab-${tab}`).setAttribute('aria-selected', String(tab === name));
    $(`admin-pane-${tab}`).hidden = tab !== name;
  }
}

async function onSession(session, previous) {
  $('admin-area').hidden = !session.admin;
  $('accounts-off').hidden = portalEnabled;
  for (const tab of ['pricing', 'usage']) $(`tab-${tab}`).hidden = !portalEnabled;
  if (session.admin) {
    if (!previous?.admin) showTab('accounts');
    await window.GufoAdmin.load();
  }
}

function setUpAdmin() {
  for (const tab of ADMIN_TABS) {
    $(`tab-${tab}`).addEventListener('click', () => showTab(tab));
  }
  window.GufoAdmin.init({
    active: () => window.GufoHeader.session.admin,
    onChange: (admin) => { if (!admin) window.GufoHeader.refresh().catch(() => {}); },
  });
}

const STATUS_TEXT = {
  pending_verification: ['status-pending', [
    'Your email address is not verified yet.',
    'Check with the admin for your verification link, open it, then come back here.']],
  pending_approval: ['status-pending', [
    'Your email address is verified. The admin still has to approve your account.']],
  approved: ['status-approved', ['Your account is approved.']],
  rejected: ['status-bad', ['The admin did not approve this account.']],
  suspended: ['status-bad', ['This account is suspended and its API keys are disabled. Contact the admin.']],
};

function renderAccount() {
  const [kind, lines] = STATUS_TEXT[me.status] ?? ['status-pending', [`Account status: ${me.status}`]];
  const extra = [];
  if (me.status === 'pending_approval' && me.signup_grant_micro > 0) {
    extra.push(`Approval comes with a one-time credit of ${fmtUsd(me.signup_grant_micro)}.`);
  }
  if (me.status === 'approved' && me.balance_micro <= 0) {
    extra.push('Your balance is used up: your keys are paused until you top up.');
  }
  $('status').className = `status ${kind}`;
  $('status').replaceChildren(...[...lines, ...extra].map((line) => el('p', {}, line)));

  $('balance').textContent = fmtUsd(me.balance_micro);
  $('balance').className = me.balance_micro <= 0 ? 'card-value bad' : 'card-value';
  $('balance-sub').textContent = me.status === 'approved' ? 'available for requests' : '';
  $('model').textContent = me.model ?? '';

  const pricing = me.pricing ?? {};
  $('pricing').tBodies[0].replaceChildren(...[
    ['Input (prompt)', pricing.input],
    ['Cached input', pricing.cached_input],
    ['Output (generated)', pricing.output],
  ].map(([label, micro]) => el('tr', {}, el('td', {}, label),
    el('td', { class: 'num' }, micro == null ? '—' : fmtUsd(micro)))));

  renderKeys();

  const base = me.api_base_url ?? '';
  $('base-url').textContent = base;
  const body = JSON.stringify({ model: me.model ?? '', messages: [{ role: 'user', content: 'Hello' }] });
  $('curl').textContent = [
    `curl ${base}/chat/completions \\`,
    '  -H "Authorization: Bearer $GUFO_API_KEY" \\',
    '  -H "Content-Type: application/json" \\',
    `  -d '${body.replaceAll("'", "'\\''")}'`,
  ].join('\n');
}

function renderKeys() {
  const approved = me.status === 'approved';
  const keys = me.keys ?? [];
  $('keys-locked').hidden = approved || keys.length > 0;
  $('keys-body').hidden = !approved && !keys.length;
  $('key-form').hidden = !approved;
  const rows = keys.map((key) => {
    const revoke = el('button', { type: 'button', class: 'link danger' }, 'Revoke');
    revoke.addEventListener('click', () => revokeKey(key));
    const state = key.enabled ? 'Active' : approved ? 'Paused: no balance' : 'Disabled';
    return el('tr', {},
      el('td', {}, key.name),
      el('td', {}, key.label ?? ''),
      el('td', {}, fmtTime(key.created)),
      el('td', {}, key.last_used ? fmtTime(key.last_used) : 'never'),
      el('td', { class: key.enabled ? 'ok' : 'bad' }, state),
      el('td', {}, revoke));
  });
  if (!rows.length) rows.push(el('tr', {}, el('td', { colspan: '6', class: 'muted' }, 'No keys yet.')));
  $('keys').tBodies[0].replaceChildren(...rows);
}

const LEDGER_KIND = {
  signup_grant: 'Welcome credit',
  deposit: 'Top-up',
  usage: 'Usage',
  admin_credit: 'Adjustment by admin',
};

async function loadLedger() {
  const { entries = [] } = await api('GET', 'ledger');
  $('ledger').tBodies[0].replaceChildren(...entries.map((entry) => el('tr', {},
    el('td', {}, fmtTime(entry.t)),
    el('td', {}, LEDGER_KIND[entry.kind] ?? entry.kind),
    el('td', { class: entry.amount_micro < 0 ? 'num' : 'num ok' },
      `${entry.amount_micro > 0 ? '+' : ''}${fmtUsd(entry.amount_micro)}`),
    el('td', { class: 'num' }, fmtUsd(entry.balance_after_micro)),
    el('td', { class: 'wrap' }, [entry.detail, entry.ref].filter(Boolean).join(' · ')))));
  $('ledger-empty').hidden = entries.length > 0;
}

// ---------------------------------------------------------------- keys

async function createKey(event) {
  event.preventDefault();
  try {
    const created = await api('POST', 'keys', { label: $('key-label').value.trim() });
    $('key-secret-name').textContent = created.name;
    $('key-secret-token').textContent = created.token;
    $('key-secret').hidden = false;
    $('key-label').value = '';
    showError('keys-error', null);
  } catch (error) {
    showError('keys-error', error);
  }
  await load().catch(() => {});
}

async function revokeKey(key) {
  const label = key.label ? `${key.name} (${key.label})` : key.name;
  if (!window.confirm(`Revoke key ${label}? Clients using it are refused within seconds.`)) return;
  try {
    await api('DELETE', `keys/${encodeURIComponent(key.name)}`);
    showError('keys-error', null);
  } catch (error) {
    showError('keys-error', error);
  }
  await load().catch(() => {});
}

// ---------------------------------------------------------------- login and signup

async function login(event) {
  event.preventDefault();
  try {
    await api('POST', 'login', { email: $('login-email').value.trim(), password: $('login-password').value });
    $('login-password').value = '';
    showError('login-error', null);
    showNotice(null);
    await load();
  } catch (error) {
    showError('login-error', error);
  }
}

async function signup(event) {
  event.preventDefault();
  const email = $('signup-email').value.trim();
  const password = $('signup-password').value;
  if (password.length < 10) {
    showError('signup-error', ERROR_TEXT.weak_password);
    return;
  }
  try {
    await api('POST', 'signup', { email, password });
    $('signup-form').reset();
    $('login-email').value = email;
    showError('signup-error', null);
    showAuthTab('signin');
    showNotice('Account created. Your email address must be verified: check with the admin for your '
      + 'verification link and open it. After that the admin approves the account; you can sign in '
      + 'meanwhile to see its status.');
  } catch (error) {
    showError('signup-error', error);
  }
}

async function logout() {
  await api('POST', 'logout').catch(() => {});
  $('key-secret-token').textContent = '';
  $('key-secret').hidden = true;
  showAuth();
}

document.addEventListener('DOMContentLoaded', () => {
  const params = new URLSearchParams(window.location.search);
  if (params.has('verified')) {
    showNotice(params.get('verified') === '1'
      ? 'Your email address is verified. The admin will now review your account.'
      : 'This verification link is invalid or has already been used.');
    window.history.replaceState(null, '', '/');
  }
  window.GufoHeader.init({
    pageUser: true,
    onAuth: openAuthForm,
    onSignOut: logout,
    onAdminTab: (tab) => {
      showTab(tab);
      $('admin-area').scrollIntoView({ block: 'start' });
    },
    onSession,
  });
  setUpAuthTabs();
  showAuthTab(params.has('signup') ? 'signup' : 'signin');
  $('login-form').addEventListener('submit', login);
  $('signup-form').addEventListener('submit', signup);
  setUpAdmin();
  $('key-form').addEventListener('submit', createKey);
  $('key-secret-copy').addEventListener('click', (event) =>
    copyText(event.currentTarget, $('key-secret-token').textContent));
  $('key-secret-done').addEventListener('click', () => {
    $('key-secret-token').textContent = '';
    $('key-secret').hidden = true;
  });
  $('base-url-copy').addEventListener('click', (event) =>
    copyText(event.currentTarget, $('base-url').textContent));
  window.GufoWallet?.init({ api, reload: () => load(), fmtUsd, parseUsd, el });
  load().catch((error) => showNotice(`Could not load the account: ${error.message}`))
    .finally(() => window.GufoHeader.refresh().catch(() => {}));
  // Usage, approvals and background-confirmed deposits show up without a reload; the header
  // refreshes the admin sign-in itself.
  setInterval(() => {
    if (me) load().catch(() => {});
  }, 30000);
});
