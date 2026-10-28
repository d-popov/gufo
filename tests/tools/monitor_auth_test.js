'use strict';

// Run with node tests/tools/monitor_auth_test.js. Exercise the actual auth handlers.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('tools/monitor/portal_static/portal.js', 'utf8');
const html = fs.readFileSync('tools/monitor/portal_static/index.html', 'utf8');
assert.match(html, /id="auth-tab-admin"[^>]*\bhidden\b/);
const nodes = new Map();
const document = { activeElement: null, querySelectorAll: () => [] };
const $ = (id) => {
  if (!nodes.has(id)) nodes.set(id, {
    id, hidden: false, value: '', textContent: '', handlers: {},
    setAttribute() {}, scrollIntoView() {},
    focus() { document.activeElement = this; },
    contains(node) { return node && (node.id.startsWith('auth') || node.id.startsWith('admin-login')); },
    addEventListener(name, callback) { this.handlers[name] = callback; },
  });
  return nodes.get(id);
};
let reply;
const context = vm.createContext({ $, document, setTimeout, window: { GufoHeader: {
  session: { admin: false }, signInAdmin: () => new Promise((resolve) => { reply = resolve; }),
} }, showError: (id, error) => { $(id).hidden = !error; $(id).textContent = error || ''; } });
vm.runInContext('let me = null; let portalEnabled = true;\n' + source.slice(
  source.indexOf('const AUTH_TABS'), source.indexOf('// ---------------------------------------------------------------- admin area')), context);
const run = (code) => vm.runInContext(code, context);
run('setUpAuthTabs(); syncAuth()');
assert.equal($('auth-tab-admin').hidden, true, 'ordinary page hides Admin');
run("openAuthForm('admin')");
assert.equal(document.activeElement.id, 'admin-login-token');
$('admin-login-token').value = 'secret';
const submit = $('admin-login-submit');
submit.focus();
$('auth').handlers.focusout({ relatedTarget: submit });
assert.equal($('auth-pane-admin').hidden, false, 'token to submit stays inside card');
$('admin-login-error').hidden = false;
$('auth').handlers.focusout({ relatedTarget: $('profile-button') });
assert.equal($('auth-tab-admin').hidden, true);
assert.equal($('admin-login-token').value, '');
assert.equal($('admin-login-error').hidden, true);
assert.equal($('auth-pane-signin').hidden, false);
run("openAuthForm('admin')");
$('auth-tab-signup').handlers.click();
assert.equal($('auth-tab-admin').hidden, true, 'Sign up dismisses Admin');
assert.equal($('auth-pane-signup').hidden, false);
run("openAuthForm('admin')");
$('auth').handlers.keydown({ key: 'Escape', preventDefault() {} });
assert.equal(document.activeElement.id, 'profile-button');
run("me = {email: 'user@example.test'}; openAuthForm('admin'); dismissAdmin()");
assert.equal($('auth').hidden, true, 'signed-in card hides on dismissal');
run("me = null; portalEnabled = false; syncAuth()");
assert.equal($('auth').hidden, true);
run("openAuthForm('admin')");
assert.equal($('auth').hidden, false, 'no-portal menu can open Admin');

(async () => {
  // Null relatedTarget with unchanged DOM focus represents a browser/wallet popup.
  $('auth').handlers.focusout({ relatedTarget: null });
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal($('auth-pane-admin').hidden, false);
  const pending = run('adminLogin({preventDefault() {}})');
  run('dismissAdmin()');
  reply('Wrong admin token.');
  await pending;
  assert.equal($('admin-login-error').hidden, true, 'late failure cannot restore dismissed error');
  console.log('Admin visibility, card focus, dismissal, Escape, no-portal and late-submit checks passed.');
})().catch((error) => { console.error(error); process.exitCode = 1; });
