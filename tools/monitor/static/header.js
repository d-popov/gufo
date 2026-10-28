'use strict';

// Page header shared by the portal page (/) and the monitor page (/monitor): the brand,
// links to both pages, and the profile menu with the portal account (Sign in, Sign up,
// Sign out) and the admin entries. Signing in, as a user or as the admin, happens in the
// portal page's sign-in card (tabs Sign in, Sign up and Admin; /?signin, /?signup and
// /?admin pick one), which calls signInAdmin() with the admin token (POST
// /api/admin/login). The monitor is the admin's alone, so its link shows only to a
// signed-in admin. Each page has an empty <header id="site-header">, calls
// GufoHeader.init() and then refresh() once it is ready for onSession; GET
// /api/admin/session says what to show. A page that loads the portal account itself
// passes pageUser and calls setUser(); otherwise refresh() reads /api/portal/me.

window.GufoHeader = (() => {
  // The product name in the brand and the page titles.
  const SITE_NAME = 'AI Inference';
  const PAGES = { portal: 'Portal', monitor: 'Monitor' };

  const $ = (id) => document.getElementById(id);
  const icon = (name) => window.GufoIcons.svg(name);

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

  function menuItem(id, iconName, label) {
    return el('button', { type: 'button', role: 'menuitem', id, class: 'menu-item' }, icon(iconName), label);
  }

  function build() {
    const page = window.location.pathname === '/monitor' ? 'monitor' : 'portal';
    document.title = `${SITE_NAME} – ${PAGES[page]}`;
    const link = (name, href, iconName) => {
      const node = el('a', { class: 'site-link', href }, icon(iconName), PAGES[name]);
      if (name === page) node.setAttribute('aria-current', 'page');
      return node;
    };
    const monitorLink = link('monitor', '/monitor', 'activity');
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
      el('div', { id: 'menu-admin-info', class: 'menu-info with-icon' }, icon('shield-check'), 'Signed in as admin'),
      menuItem('menu-signin', 'log-in', 'Sign in'),
      menuItem('menu-signup', 'user-plus', 'Sign up'),
      menuItem('menu-admin-signin', 'shield-check', 'Admin sign-in'),
      menuItem('menu-admin-accounts', 'users', 'Admin: accounts'),
      menuItem('menu-admin-keys', 'key-round', 'Admin: API keys'),
      menuItem('menu-admin-pricing', 'tag', 'Admin: pricing'),
      menuItem('menu-admin-usage', 'chart-column', 'Admin: usage'),
      menuItem('menu-admin-deposits', 'landmark', 'Admin: deposits'),
      menuItem('menu-admin-signout', 'log-out', 'Sign out of admin'),
      menuItem('menu-signout', 'log-out', 'Sign out'));
    menu.hidden = true;

    $('site-header').replaceChildren(
      el('div', { class: 'site-bar' },
        el('a', { class: 'brand', href: '/' }, icon('brand'), SITE_NAME),
        el('nav', { class: 'site-nav', 'aria-label': 'Pages' }, link('portal', '/', 'wallet'), monitorLink),
        el('div', { class: 'profile' }, button, menu)));
  }

  function render() {
    const button = $('profile-button');
    button.replaceChildren(user ? user.email.slice(0, 1).toUpperCase() : icon('user'));
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
  }

  function openMenu(open) {
    $('profile-menu').hidden = !open;
    $('profile-button').setAttribute('aria-expanded', String(open));
    if (open) $('profile-menu').querySelector('.menu-item:not([hidden])')?.focus();
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

  // The admin sign-in with TOKEN; null once signed in (the session is then refreshed),
  // otherwise what went wrong.
  async function signInAdmin(token) {
    try {
      const response = await adminRequest('/api/admin/login', { token });
      if (!response.ok) {
        return response.status === 401 ? 'Wrong admin token.'
          : response.status === 429 ? 'Too many attempts; try again in a minute.'
            : `Sign-in failed (HTTP ${response.status}).`;
      }
    } catch (error) {
      return String(error.message ?? error);
    }
    await refresh();
    return null;
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

  // TAB: 'signin', 'signup' or 'admin', the portal page's sign-in card.
  function auth(tab) {
    openMenu(false);
    if (options.onAuth) options.onAuth(tab);
    else window.location.assign(`/?${tab}`);
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
    $('menu-admin-signin').addEventListener('click', () => auth('admin'));
    $('menu-signout').addEventListener('click', userSignOut);
    for (const name of ['accounts', 'keys', 'pricing', 'usage', 'deposits']) {
      $(`menu-admin-${name}`).addEventListener('click', () => {
        openMenu(false);
        options.onAdminTab?.(name);
      });
    }
    $('menu-admin-signout').addEventListener('click', adminSignOut);
  }

  // Builds the header and puts icons in the page's static markup; refresh() then loads
  // the session, which this repeats every 30 s so the admin sign-in ending shows up
  // without a reload.
  function init(settings = {}) {
    options = settings;
    window.GufoIcons.hydrate();
    build();
    wire();
    render();
    setInterval(() => refresh().catch(() => {}), 30000);
  }

  return {
    init,
    refresh,
    setUser,
    signInAdmin,
    fmtUsd,
    get session() { return session; },
  };
})();
