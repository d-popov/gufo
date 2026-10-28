'use strict';

// Page header shared by the portal page (/) and the monitor page (/monitor): the brand,
// links to both pages, and the profile menu with the portal account (Sign in, Sign up,
// Sign out) and the admin sign-in (POST /api/admin/login with the admin token). The
// monitor is the admin's alone, so its link shows only to a signed-in admin. Each page has
// an empty <header id="site-header">, calls GufoHeader.init() and then refresh() once it
// is ready for onSession; GET /api/admin/session says what to show. A page that loads the
// portal account itself passes pageUser and calls setUser(); otherwise refresh() reads
// /api/portal/me.

window.GufoHeader = (() => {
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

  // Integer micro-USD as dollars with at least two decimals.
  function fmtUsd(micro) {
    const sign = micro < 0 ? '-' : '';
    const abs = Math.abs(micro);
    const whole = Math.floor(abs / 1e6).toLocaleString('en-US');
    let frac = String(abs % 1e6).padStart(6, '0').replace(/0+$/, '');
    if (frac.length < 2) frac = frac.padEnd(2, '0');
    return `${sign}$${whole}.${frac}`;
  }

  const USER_STATUS = {
    pending_verification: 'Email not verified yet',
    pending_approval: 'Awaiting approval',
    approved: 'Approved',
    rejected: 'Not approved',
    suspended: 'Suspended',
  };

  // { pageUser, onAuth(tab), onSignOut(), onAdminTab(name), onSession(session, previous) }
  let options = {};
  let session = { admin: false };
  let loaded = false;
  let user = null;
  let portalEnabled = true;

  function menuItem(id, label, href) {
    return href ? el('a', { role: 'menuitem', id, class: 'menu-item', href }, label)
      : el('button', { type: 'button', role: 'menuitem', id, class: 'menu-item' }, label);
  }

  function build() {
    const page = window.location.pathname === '/monitor' ? 'monitor' : 'portal';
    const link = (name, href, label) => {
      const node = el('a', { class: 'site-link', href }, label);
      if (name === page) node.setAttribute('aria-current', 'page');
      return node;
    };
    const monitorLink = link('monitor', '/monitor', 'Monitor');
    monitorLink.id = 'site-link-monitor';
    monitorLink.hidden = true;
    const button = el('button', {
      type: 'button', id: 'profile-button', class: 'avatar anon', 'aria-haspopup': 'menu',
      'aria-expanded': 'false', 'aria-controls': 'profile-menu', 'aria-label': 'Account menu' });
    const menu = el('div', { id: 'profile-menu', class: 'menu', role: 'menu' },
      el('div', { id: 'menu-user', class: 'menu-info' },
        el('div', { id: 'menu-email', class: 'menu-email' }),
        el('div', { id: 'menu-status', class: 'menu-muted' }),
        el('div', { id: 'menu-balance' })),
      el('div', { id: 'menu-admin-info', class: 'menu-info' }, 'Signed in as admin'),
      menuItem('menu-signin', 'Sign in'),
      menuItem('menu-signup', 'Sign up'),
      menuItem('menu-admin-signin', 'Admin sign-in'),
      menuItem('menu-admin-accounts', 'Admin: accounts'),
      menuItem('menu-admin-keys', 'Admin: API keys'),
      menuItem('menu-admin-pricing', 'Admin: pricing'),
      menuItem('menu-admin-usage', 'Admin: usage'),
      menuItem('menu-admin-deposits', 'Admin: deposits'),
      menuItem('menu-admin-signout', 'Sign out of admin'),
      menuItem('menu-signout', 'Sign out'));
    menu.hidden = true;

    const token = el('input', { id: 'admin-login-token', type: 'password', autocomplete: 'off' });
    token.required = true;
    const error = el('p', { id: 'admin-login-error', class: 'header-error', role: 'alert' });
    error.hidden = true;
    const form = el('form', { id: 'admin-login-form', class: 'header-login' },
      el('h2', {}, 'Admin sign-in'),
      el('label', { for: 'admin-login-token' }, 'Admin token'),
      token,
      el('div', { class: 'header-actions' },
        el('button', { type: 'submit', class: 'header-primary' }, 'Sign in'),
        el('button', { type: 'button', id: 'admin-login-cancel', class: 'header-link' }, 'Cancel')),
      error);
    form.hidden = true;

    $('site-header').replaceChildren(
      el('div', { class: 'site-bar' },
        el('a', { class: 'brand', href: '/' }, 'Gufo'),
        el('nav', { class: 'site-nav', 'aria-label': 'Pages' }, link('portal', '/', 'Portal'), monitorLink),
        el('div', { class: 'profile' }, button, menu)),
      form);
  }

  function render() {
    const button = $('profile-button');
    button.textContent = user ? user.email.slice(0, 1).toUpperCase() : '';
    button.classList.toggle('anon', !user);
    button.classList.toggle('is-admin', session.admin);
    button.title = user ? user.email : session.admin ? 'Admin' : 'Sign in';
    $('menu-user').hidden = !user;
    if (user) {
      $('menu-email').textContent = user.email;
      $('menu-status').textContent = USER_STATUS[user.status] ?? user.status;
      $('menu-balance').textContent = `Balance ${fmtUsd(user.balance_micro)}`;
    }
    $('menu-signin').hidden = !!user || !portalEnabled;
    $('menu-signup').hidden = !!user || !portalEnabled;
    $('menu-signout').hidden = !user;
    $('menu-admin-signin').hidden = session.admin;
    for (const id of ['menu-admin-info', 'menu-admin-accounts', 'menu-admin-keys']) {
      $(id).hidden = !session.admin;
    }
    for (const id of ['menu-admin-pricing', 'menu-admin-usage', 'menu-admin-deposits']) {
      $(id).hidden = !session.admin || !portalEnabled;  // need the credits portal
    }
    $('menu-admin-signout').hidden = !session.admin;
    $('site-link-monitor').hidden = !session.admin;
    if (session.admin) $('admin-login-form').hidden = true;
  }

  function openMenu(open) {
    $('profile-menu').hidden = !open;
    $('profile-button').setAttribute('aria-expanded', String(open));
    if (open) $('profile-menu').querySelector('.menu-item:not([hidden])')?.focus();
  }

  function showLoginError(text) {
    $('admin-login-error').textContent = text ?? '';
    $('admin-login-error').hidden = !text;
  }

  async function loadUser() {
    const response = await fetch('/api/portal/me', { credentials: 'same-origin', cache: 'no-store' });
    portalEnabled = response.status !== 404;
    user = response.ok ? await response.json() : null;
  }

  // Reads the session (and the portal account, unless the page owns it) and tells the page.
  async function refresh() {
    const previous = loaded ? session : null;
    const [response] = await Promise.all([
      fetch('/api/admin/session', { credentials: 'same-origin', cache: 'no-store' }),
      options.pageUser ? null : loadUser().catch(() => {}),
    ]);
    session = response.ok ? await response.json() : { admin: false };
    loaded = true;
    render();
    await options.onSession?.(session, previous);
  }

  function setUser(account, enabled = true) {
    user = account;
    portalEnabled = enabled;
    render();
  }

  async function adminRequest(path, body) {
    return fetch(path, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) });
  }

  async function adminSignIn(event) {
    event.preventDefault();
    try {
      const response = await adminRequest('/api/admin/login', { token: $('admin-login-token').value });
      if (!response.ok) {
        showLoginError(response.status === 401 ? 'Wrong admin token.'
          : response.status === 429 ? 'Too many attempts; try again in a minute.'
            : `Sign-in failed (HTTP ${response.status}).`);
        return;
      }
      $('admin-login-token').value = '';
      showLoginError(null);
      $('admin-login-form').hidden = true;
    } catch (error) {
      showLoginError(String(error.message ?? error));
      return;
    }
    await refresh();
  }

  // The monitor page is the admin's alone: signing out there leaves for the portal page.
  async function adminSignOut() {
    openMenu(false);
    await adminRequest('/api/admin/logout').catch(() => {});
    if (window.location.pathname === '/monitor') {
      window.location.assign('/');
      return;
    }
    await refresh();
  }

  async function userSignOut() {
    openMenu(false);
    if (options.onSignOut) return options.onSignOut();
    await fetch('/api/portal/logout', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-Gufo-Portal': '1' }, body: '{}' }).catch(() => {});
    setUser(null, portalEnabled);
  }

  function auth(tab) {
    openMenu(false);
    if (options.onAuth) options.onAuth(tab);
    else window.location.assign(tab === 'signup' ? '/?signup' : '/?signin');
  }

  function wire() {
    $('profile-button').addEventListener('click', (event) => {
      event.stopPropagation();
      openMenu($('profile-menu').hidden);
    });
    document.addEventListener('click', (event) => {
      if (!$('profile-menu').hidden && !$('profile-menu').contains(event.target)) openMenu(false);
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !$('profile-menu').hidden) {
        openMenu(false);
        $('profile-button').focus();
      }
    });
    $('menu-signin').addEventListener('click', () => auth('signin'));
    $('menu-signup').addEventListener('click', () => auth('signup'));
    $('menu-signout').addEventListener('click', userSignOut);
    $('menu-admin-signin').addEventListener('click', () => {
      openMenu(false);
      $('admin-login-form').hidden = false;
      $('admin-login-token').focus();
    });
    $('admin-login-cancel').addEventListener('click', () => {
      $('admin-login-form').hidden = true;
      showLoginError(null);
    });
    $('admin-login-form').addEventListener('submit', adminSignIn);
    for (const name of ['accounts', 'keys', 'pricing', 'usage', 'deposits']) {
      $(`menu-admin-${name}`).addEventListener('click', () => {
        openMenu(false);
        options.onAdminTab?.(name);
      });
    }
    $('menu-admin-signout').addEventListener('click', adminSignOut);
  }

  // Builds the header; refresh() then loads the session, which this repeats every 30 s so
  // the admin sign-in ending shows up without a reload.
  function init(settings = {}) {
    options = settings;
    build();
    wire();
    render();
    setInterval(() => refresh().catch(() => {}), 30000);
  }

  return {
    init,
    refresh,
    setUser,
    fmtUsd,
    get session() { return session; },
  };
})();
